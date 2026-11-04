from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast
from urllib.parse import urlencode

import httpx

from src.core.config import Settings, settings
from src.core.exceptions import APIError
from src.core.uow import UnitOfWork
from src.db.models import PaymentOrder, User
from src.repositories.payment_order_repository import PaymentOrderRepository
from src.repositories.payment_webhook_event_repository import PaymentWebhookEventRepository
from src.repositories.user_access_pass_repository import UserAccessPassRepository
from src.repositories.user_repository import UserRepository
from src.schemas.payment import (
    CreateWechatCheckoutSessionResponse,
    PaymentOrderResponse,
    WechatPaymentProductListResponse,
    WechatPaymentProductResponse,
)
from src.services.subscription_service import SubscriptionService

logger = logging.getLogger(__name__)

_DODO_HTTP_TIMEOUT_SECONDS = 15.0
_DODO_API_BASE_URLS = {
    "live_mode": "https://live.dodopayments.com",
    "test_mode": "https://test.dodopayments.com",
}
_DODO_WECHAT_PAYMENT_METHOD = "we_chat_pay"
_CHANNEL = "wechat_pay"
_BILLING_CURRENCY = "CNY"
_PRICE_NOTE = "微信支付将以人民币结算，最终金额以结账页为准"
_PRODUCT_CACHE_TTL_SECONDS = 60.0
_product_cache: dict[str, dict[str, object]] = {}
_product_cache_expiry: float = 0.0
_product_cache_lock = asyncio.Lock()


@dataclass(frozen=True)
class WechatProductDefinition:
    product_id: str
    name: str
    tier: str
    duration_days: int
    base_price_minor: int
    base_price_currency: str


def _normalized_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _parse_iso_datetime(value: object) -> datetime | None:
    normalized = _normalized_text(value)
    if normalized is None:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_webhook_timestamp(value: str | None) -> datetime | None:
    normalized = _normalized_text(value)
    if normalized is None:
        return None
    try:
        return datetime.fromtimestamp(int(normalized), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_order_uuid(value: object) -> uuid.UUID | None:
    normalized = _normalized_text(value)
    if normalized is None:
        return None
    try:
        return uuid.UUID(normalized)
    except ValueError:
        return None


def _resolve_dodo_base_url(environment: str | None) -> str:
    normalized = (environment or "").strip().lower()
    return _DODO_API_BASE_URLS.get(normalized, _DODO_API_BASE_URLS["test_mode"])


def _tier_rank(tier: str) -> int:
    if tier == "pro":
        return 2
    if tier == "plus":
        return 1
    return 0


def _format_usd_minor(amount_minor: int, currency: str) -> str:
    return f"${amount_minor / 100:.2f}" if currency.upper() == "USD" else f"{amount_minor}"


async def _fetch_dodo_products(
    product_ids: list[str],
    settings_obj: Settings,
) -> dict[str, dict[str, object]]:
    api_key = _normalized_text(settings_obj.DODO_PAYMENTS_API_KEY)
    if api_key is None:
        return {}

    base_url = _resolve_dodo_base_url(settings_obj.DODO_PAYMENTS_ENVIRONMENT)
    result: dict[str, dict[str, object]] = {}

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=_DODO_HTTP_TIMEOUT_SECONDS,
    ) as client:
        tasks = [client.get(f"/products/{pid}") for pid in product_ids]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for pid, response in zip(product_ids, responses):
            if isinstance(response, BaseException):
                logger.warning(
                    "Failed to fetch product from Dodo",
                    extra={"product_id": pid, "error": str(response)},
                )
                continue
            try:
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.warning(
                    "Failed to fetch product from Dodo",
                    extra={"product_id": pid, "error": str(exc)},
                )
                continue
            if not isinstance(data, dict):
                continue
            price_obj = data.get("price")
            if not isinstance(price_obj, dict):
                continue
            price_minor = price_obj.get("price")
            currency = _normalized_text(price_obj.get("currency"))
            name = _normalized_text(data.get("name"))
            if isinstance(price_minor, int) and currency:
                result[pid] = {
                    "price_minor": price_minor,
                    "currency": currency,
                    "name": name or "",
                }

    return result


class WechatPaymentService:
    def __init__(
        self,
        order_repo: PaymentOrderRepository,
        webhook_event_repo: PaymentWebhookEventRepository,
        user_access_pass_repo: UserAccessPassRepository,
        user_repo: UserRepository,
        subscription_service: SubscriptionService,
        uow: UnitOfWork,
        settings_obj: Settings = settings,
    ) -> None:
        self._order_repo = order_repo
        self._webhook_event_repo = webhook_event_repo
        self._user_access_pass_repo = user_access_pass_repo
        self._user_repo = user_repo
        self._subscription_service = subscription_service
        self._uow = uow
        self._settings = settings_obj

    async def _refresh_product_cache(self) -> None:
        global _product_cache, _product_cache_expiry
        if time.monotonic() < _product_cache_expiry:
            return

        async with _product_cache_lock:
            if time.monotonic() < _product_cache_expiry:
                return

            product_ids = [
                pid
                for pid in (
                    self._settings.DODO_WECHAT_PLUS_30D_PRODUCT_ID,
                    self._settings.DODO_WECHAT_PRO_30D_PRODUCT_ID,
                    self._settings.DODO_WECHAT_PLUS_365D_PRODUCT_ID,
                    self._settings.DODO_WECHAT_PRO_365D_PRODUCT_ID,
                )
                if _normalized_text(pid) is not None
            ]

            fetched = await _fetch_dodo_products(product_ids, self._settings)
            if len(fetched) == len(product_ids):
                _product_cache = fetched
                _product_cache_expiry = time.monotonic() + _PRODUCT_CACHE_TTL_SECONDS

    async def list_products(self) -> WechatPaymentProductListResponse:
        await self._refresh_product_cache()
        return WechatPaymentProductListResponse(
            items=[self._to_product_response(item) for item in self._get_product_definitions()]
        )

    async def create_checkout_session(
        self,
        *,
        current_user: User,
        product_id: str,
    ) -> CreateWechatCheckoutSessionResponse:
        if not bool(getattr(self._settings, "BILLING_CHECKOUT_ENABLED", True)):
            raise APIError(
                status_code=409,
                code="resource_conflict",
                message="resource conflict: checkout is temporarily unavailable",
            )
        await self._refresh_product_cache()
        product = self._get_product_or_raise(product_id)
        effective_entitlement = await self._subscription_service.get_effective_entitlement(
            current_user
        )
        if _tier_rank(product.tier) <= _tier_rank(effective_entitlement.tier):
            logger.info(
                "Wechat checkout denied because current entitlement already covers the request",
                extra={
                    "event": "payment.wechat_checkout_denied_conflict",
                    "user_id": str(current_user.id),
                    "effective_tier": effective_entitlement.tier,
                    "requested_tier": product.tier,
                    "product_id": product.product_id,
                },
            )
            raise APIError(
                status_code=409,
                code="resource_conflict",
                message=(
                    "resource conflict: current effective tier already covers requested purchase; "
                    f"effective_tier={effective_entitlement.tier}; requested_tier={product.tier}"
                ),
            )

        order = PaymentOrder(
            user_id=current_user.id,
            dodo_product_id=product.product_id,
            tier=product.tier,
            duration_days=product.duration_days,
            base_price_minor=product.base_price_minor,
            base_price_currency=product.base_price_currency,
            billing_currency=_BILLING_CURRENCY,
            dodo_customer_id=current_user.dodo_customer_id,
            status="created",
        )

        async with self._uow.transactional():
            await self._order_repo.create(order)

        logger.info(
            "Creating Dodo WeChat checkout session",
            extra={
                "event": "payment.wechat_checkout_requested",
                "user_id": str(current_user.id),
                "product_id": product.product_id,
            },
        )

        checkout_payload = self._build_checkout_payload(order=order, current_user=current_user)
        try:
            checkout_response = await self._create_dodo_checkout(checkout_payload)
        except httpx.TimeoutException as exc:
            await self._mark_checkout_creation_failed(order, {"error": str(exc)})
            raise APIError(
                status_code=502,
                code="external_timeout",
                message="external timeout: Dodo checkout session creation timed out",
            ) from exc
        except httpx.HTTPError as exc:
            await self._mark_checkout_creation_failed(order, {"error": str(exc)})
            raise APIError(
                status_code=502,
                code="external_error",
                message="external error: failed to create Dodo checkout session",
            ) from exc

        checkout_session_id = _normalized_text(checkout_response.get("session_id"))
        checkout_url = _normalized_text(checkout_response.get("checkout_url"))
        if checkout_session_id is None or checkout_url is None:
            await self._mark_checkout_creation_failed(order, {"response": checkout_response})
            raise APIError(
                status_code=502,
                code="external_error",
                message="external error: Dodo checkout response is missing required fields",
            )

        async with self._uow.transactional():
            await self._order_repo.apply_update(
                order,
                update_data={
                    "dodo_checkout_session_id": checkout_session_id,
                    "checkout_url": checkout_url,
                    "checkout_payload_json": checkout_payload,
                    "status": "checkout_created",
                },
            )

        logger.info(
            "Dodo WeChat checkout session created",
            extra={
                "event": "payment.wechat_checkout_created",
                "order_id": str(order.id),
                "product_id": product.product_id,
                "checkout_session_id": checkout_session_id,
            },
        )

        return CreateWechatCheckoutSessionResponse(
            order_id=str(order.id),
            checkout_url=checkout_url,
            checkout_session_id=checkout_session_id,
            product_id=product.product_id,
            tier=product.tier,
            duration_days=product.duration_days,
            billing_currency=_BILLING_CURRENCY,
            channel="wechat_pay",
        )

    async def get_order_for_user(
        self,
        *,
        order_id: uuid.UUID,
        current_user: User,
    ) -> PaymentOrderResponse:
        order = await self._order_repo.get_by_id_for_user(order_id=order_id, user_id=current_user.id)
        if order is None:
            raise APIError(
                status_code=404,
                code="resource_not_found",
                message=f"resource not found: payment order does not exist; order_id={order_id}",
            )
        return self._to_order_response(order)

    async def list_orders_for_user(
        self,
        *,
        current_user: User,
        channel: str | None,
        skip: int,
        limit: int,
    ) -> list[PaymentOrderResponse]:
        orders = await self._order_repo.list_by_user(
            user_id=current_user.id,
            channel=channel,
            skip=skip,
            limit=limit,
        )
        return [self._to_order_response(order) for order in orders]

    async def apply_webhook_event(
        self,
        event: object,
        *,
        webhook_id: str,
        webhook_timestamp: str,
    ) -> bool:
        if not isinstance(event, dict):
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: webhook payload must be an object",
            )

        if await self._webhook_event_repo.get_by_webhook_id(webhook_id) is not None:
            return False

        event_payload = cast(dict[str, object], event)
        event_type = _normalized_text(event_payload.get("type"))
        if event_type is None:
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: webhook payload is missing event type",
            )

        data = event_payload.get("data")
        if not isinstance(data, dict):
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: webhook payload is missing data object",
            )
        payload_data = cast(dict[str, object], data)

        async with self._uow.transactional():
            if await self._webhook_event_repo.get_by_webhook_id(webhook_id) is not None:
                return False

            event_record = await self._webhook_event_repo.create(
                webhook_id=webhook_id,
                event_type=event_type,
                webhook_timestamp=_parse_webhook_timestamp(webhook_timestamp),
                customer_id=self._extract_customer_id(payload_data),
                payment_id=_normalized_text(payload_data.get("payment_id")),
                refund_id=_normalized_text(payload_data.get("refund_id")),
                dodo_product_id=self._extract_product_id(payload_data),
                payload_json=event_payload,
            )

            order = await self._resolve_order_from_event(payload_data)
            if order is None:
                _ = await self._webhook_event_repo.mark_processed(
                    event_record,
                    processing_status="ignored_unknown_order",
                )
                logger.info(
                    "Ignored payment webhook because order was not found",
                    extra={
                        "event": "payment.webhook_processed",
                        "processing_status": "ignored_unknown_order",
                        "webhook_id": webhook_id,
                        "event_type": event_type,
                    },
                )
                return True

            processing_status = await self._apply_event_to_order(
                order=order,
                event_type=event_type,
                payload_data=payload_data,
                event_payload=event_payload,
                webhook_timestamp=webhook_timestamp,
            )
            _ = await self._webhook_event_repo.mark_processed(
                event_record,
                processing_status=processing_status,
            )

            logger.info(
                "Processed payment webhook",
                extra={
                    "event": "payment.webhook_processed",
                    "processing_status": processing_status,
                    "webhook_id": webhook_id,
                    "event_type": event_type,
                    "order_id": str(order.id),
                },
            )
        return True

    def _get_product_definitions(self) -> list[WechatProductDefinition]:
        definitions = [
            (
                self._settings.DODO_WECHAT_PLUS_30D_PRODUCT_ID,
                "ParlaSoul Plus 30 Days",
                "plus",
                30,
            ),
            (
                self._settings.DODO_WECHAT_PRO_30D_PRODUCT_ID,
                "ParlaSoul Pro 30 Days",
                "pro",
                30,
            ),
            (
                self._settings.DODO_WECHAT_PLUS_365D_PRODUCT_ID,
                "ParlaSoul Plus 365 Days",
                "plus",
                365,
            ),
            (
                self._settings.DODO_WECHAT_PRO_365D_PRODUCT_ID,
                "ParlaSoul Pro 365 Days",
                "pro",
                365,
            ),
        ]
        items: list[WechatProductDefinition] = []
        for product_id, name, tier, duration_days in definitions:
            normalized_product_id = _normalized_text(product_id)
            if normalized_product_id is None:
                raise APIError(
                    status_code=500,
                    code="internal_error",
                    message=(
                        "internal error: WeChat payment product mapping is incomplete; "
                        f"missing_product={name}"
                    ),
                )

            cached = _product_cache.get(normalized_product_id)
            if cached is None:
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message=(
                        "external error: unable to fetch product pricing from Dodo; "
                        f"product_id={normalized_product_id}"
                    ),
                )
            base_price_minor = int(cached["price_minor"])
            base_price_currency = str(cached["currency"])
            display_name = str(cached.get("name") or name)

            items.append(
                WechatProductDefinition(
                    product_id=normalized_product_id,
                    name=display_name,
                    tier=tier,
                    duration_days=duration_days,
                    base_price_minor=base_price_minor,
                    base_price_currency=base_price_currency,
                )
            )
        return items

    def _get_product_or_raise(self, product_id: str) -> WechatProductDefinition:
        normalized = _normalized_text(product_id)
        if normalized is None:
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: product_id is required",
            )
        for item in self._get_product_definitions():
            if item.product_id == normalized:
                return item
        raise APIError(
            status_code=400,
            code="invalid_param",
            message=f"invalid request: unsupported product_id; product_id={product_id}",
        )

    def _to_product_response(self, product: WechatProductDefinition) -> WechatPaymentProductResponse:
        return WechatPaymentProductResponse(
            product_id=product.product_id,
            name=product.name,
            tier=product.tier,
            duration_days=product.duration_days,
            base_price_minor=product.base_price_minor,
            base_price_currency=product.base_price_currency,
            base_price_display=_format_usd_minor(
                product.base_price_minor,
                product.base_price_currency,
            ),
            billing_currency=_BILLING_CURRENCY,
            channel="wechat_pay",
            price_note=_PRICE_NOTE,
        )

    def _build_checkout_payload(
        self,
        *,
        order: PaymentOrder,
        current_user: User,
    ) -> dict[str, object]:
        return_url = self._build_return_url(order.id)
        customer_name = (
            current_user.username
            or current_user.display_name
            or current_user.email.split("@", 1)[0]
        )
        customer_payload: dict[str, str]
        if current_user.dodo_customer_id:
            customer_payload = {"customer_id": current_user.dodo_customer_id}
        else:
            customer_payload = {"email": current_user.email, "name": customer_name}

        return {
            "product_cart": [{"product_id": order.dodo_product_id, "quantity": 1}],
            "allowed_payment_method_types": [_DODO_WECHAT_PAYMENT_METHOD],
            "billing_currency": _BILLING_CURRENCY,
            "return_url": return_url,
            "customer": customer_payload,
            "metadata": {
                "order_id": str(order.id),
                "product_id": order.dodo_product_id,
            },
        }

    def _build_return_url(self, order_id: uuid.UUID) -> str:
        query = urlencode(
            {
                "checkout": "success",
                "channel": _CHANNEL,
                "order_id": str(order_id),
            }
        )
        return f"{self._settings.DODO_PAYMENTS_WEB_APP_BASE_URL.rstrip('/')}/billing?{query}"

    async def _create_dodo_checkout(self, payload: Mapping[str, object]) -> dict[str, object]:
        api_key = _normalized_text(self._settings.DODO_PAYMENTS_API_KEY)
        if api_key is None:
            raise APIError(
                status_code=500,
                code="internal_error",
                message="internal error: Dodo API key is not configured",
            )

        base_url = _resolve_dodo_base_url(self._settings.DODO_PAYMENTS_ENVIRONMENT)
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=_DODO_HTTP_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post("/checkouts", json=dict(payload))
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message="external error: Dodo checkout response is invalid",
                )
            return cast(dict[str, object], data)

    async def _mark_checkout_creation_failed(
        self,
        order: PaymentOrder,
        details: Mapping[str, object],
    ) -> None:
        async with self._uow.transactional():
            await self._order_repo.apply_update(
                order,
                update_data={
                    "status": "failed",
                    "last_event_payload_json": dict(details),
                },
            )

    async def _resolve_order_from_event(
        self,
        payload_data: Mapping[str, object],
    ) -> PaymentOrder | None:
        metadata = payload_data.get("metadata")
        if isinstance(metadata, dict):
            order_id = _parse_order_uuid(metadata.get("order_id"))
            if order_id is not None:
                order = await self._order_repo.get_by_id(order_id)
                if order is not None:
                    return order

        payment_id = _normalized_text(payload_data.get("payment_id"))
        if payment_id is not None:
            order = await self._order_repo.get_by_payment_id(payment_id)
            if order is not None:
                return order

        checkout_session_id = _normalized_text(payload_data.get("checkout_session_id"))
        if checkout_session_id is not None:
            return await self._order_repo.get_by_checkout_session_id(checkout_session_id)
        return None

    async def _apply_event_to_order(
        self,
        *,
        order: PaymentOrder,
        event_type: str,
        payload_data: Mapping[str, object],
        event_payload: Mapping[str, object],
        webhook_timestamp: str,
    ) -> str:
        update_data: dict[str, object] = {
            "last_event_payload_json": dict(event_payload),
        }

        customer_id = self._extract_customer_id(payload_data)
        payment_id = _normalized_text(payload_data.get("payment_id"))
        refund_id = _normalized_text(payload_data.get("refund_id"))
        checkout_session_id = _normalized_text(payload_data.get("checkout_session_id"))
        charged_total_minor = payload_data.get("total_amount")
        charged_currency = _normalized_text(payload_data.get("currency"))
        settlement_total_minor = payload_data.get("settlement_amount")
        settlement_currency = _normalized_text(payload_data.get("settlement_currency"))
        product_id = self._extract_product_id(payload_data) or order.dodo_product_id

        if customer_id is not None:
            update_data["dodo_customer_id"] = customer_id
        if payment_id is not None:
            update_data["dodo_payment_id"] = payment_id
        if refund_id is not None:
            update_data["dodo_refund_id"] = refund_id
        if checkout_session_id is not None:
            update_data["dodo_checkout_session_id"] = checkout_session_id
        if isinstance(charged_total_minor, int):
            update_data["charged_total_minor"] = charged_total_minor
        if charged_currency is not None:
            update_data["charged_currency"] = charged_currency
        if isinstance(settlement_total_minor, int):
            update_data["settlement_total_minor"] = settlement_total_minor
        if settlement_currency is not None:
            update_data["settlement_currency"] = settlement_currency
        if product_id:
            update_data["dodo_product_id"] = product_id

        resolved_event_time = (
            _parse_iso_datetime(payload_data.get("updated_at"))
            or _parse_iso_datetime(payload_data.get("created_at"))
            or _parse_iso_datetime(event_payload.get("timestamp"))
            or _parse_webhook_timestamp(webhook_timestamp)
            or datetime.now(timezone.utc)
        )

        status = order.status
        if event_type == "payment.processing":
            if order.status not in {"succeeded", "refunded"}:
                status = "pending"
        elif event_type == "payment.succeeded":
            status = "succeeded"
            update_data["paid_at"] = order.paid_at or resolved_event_time
        elif event_type == "payment.failed":
            if order.status not in {"succeeded", "refunded"}:
                status = "failed"
        elif event_type == "payment.cancelled":
            if order.status not in {"succeeded", "refunded"}:
                status = "cancelled"
        elif event_type == "refund.succeeded":
            status = "refunded"
            update_data["refunded_at"] = resolved_event_time

        update_data["status"] = status
        await self._order_repo.apply_update(order, update_data=update_data)

        if customer_id is not None and order.user_id:
            user = await self._user_repo.get_by_id(order.user_id)
            if user is not None and user.dodo_customer_id != customer_id:
                user.dodo_customer_id = customer_id

        if event_type == "payment.succeeded":
            paid_at = cast(datetime, update_data["paid_at"])
            access_pass = await self._user_access_pass_repo.upsert_active_pass(
                user_id=order.user_id,
                source_order_id=order.id,
                channel=order.channel,
                product_id=order.dodo_product_id,
                tier=order.tier,
                starts_at=paid_at,
                ends_at=paid_at + timedelta(days=int(order.duration_days)),
            )
            logger.info(
                "Activated one-time access pass",
                extra={
                    "event": "entitlement.pass_activated",
                    "user_id": str(order.user_id),
                    "order_id": str(order.id),
                    "product_id": order.dodo_product_id,
                    "tier": order.tier,
                },
            )
            _ = access_pass
        elif event_type == "refund.succeeded":
            access_pass = await self._user_access_pass_repo.get_by_source_order_id(order.id)
            if access_pass is not None:
                await self._user_access_pass_repo.mark_status(access_pass, status="refunded")
                logger.info(
                    "Refunded one-time access pass",
                    extra={
                        "event": "entitlement.pass_refunded",
                        "user_id": str(order.user_id),
                        "order_id": str(order.id),
                        "product_id": order.dodo_product_id,
                        "tier": order.tier,
                    },
                )
        return "processed"

    @staticmethod
    def _extract_customer_id(payload_data: Mapping[str, object]) -> str | None:
        customer_id = _normalized_text(payload_data.get("customer_id"))
        if customer_id:
            return customer_id
        customer = payload_data.get("customer")
        if isinstance(customer, dict):
            return _normalized_text(customer.get("customer_id"))
        return None

    @staticmethod
    def _extract_product_id(payload_data: Mapping[str, object]) -> str | None:
        direct_product_id = _normalized_text(payload_data.get("product_id"))
        if direct_product_id:
            return direct_product_id

        metadata = payload_data.get("metadata")
        if isinstance(metadata, dict):
            product_id = _normalized_text(metadata.get("product_id"))
            if product_id:
                return product_id

        product_cart = payload_data.get("product_cart")
        if isinstance(product_cart, list):
            for item in product_cart:
                if isinstance(item, dict):
                    product_id = _normalized_text(item.get("product_id"))
                    if product_id:
                        return product_id
        return None

    @staticmethod
    def _to_order_response(order: PaymentOrder) -> PaymentOrderResponse:
        created_at = order.created_at or datetime.now(timezone.utc)
        return PaymentOrderResponse(
            id=str(order.id),
            status=order.status,
            product_id=order.dodo_product_id,
            tier=order.tier,
            duration_days=int(order.duration_days),
            channel="wechat_pay",
            billing_currency=order.billing_currency,
            paid_at=order.paid_at,
            refunded_at=order.refunded_at,
            created_at=created_at,
            charged_total_minor=(
                int(order.charged_total_minor) if order.charged_total_minor is not None else None
            ),
            charged_currency=order.charged_currency,
            settlement_total_minor=(
                int(order.settlement_total_minor)
                if order.settlement_total_minor is not None
                else None
            ),
            settlement_currency=order.settlement_currency,
        )
