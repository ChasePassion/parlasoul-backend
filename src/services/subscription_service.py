from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

import httpx

from src.core.config import Settings, settings
from src.core.exceptions import APIError
from src.core.uow import UnitOfWork
from src.db.models import User, UserAccessPass, UserSettings
from src.repositories.subscription_webhook_event_repository import (
    SubscriptionWebhookEventRepository,
)
from src.repositories.user_access_pass_repository import UserAccessPassRepository
from src.repositories.user_repository import UserRepository
from src.schemas.payment import UserAccessPassResponse
from src.schemas.user import (
    EffectiveEntitlementSource,
    UserEntitlementFeatures,
    UserEntitlementSettings,
    UserEntitlementsResponse,
)
from src.schemas.user_settings import UserSettingsResponse

SubscriptionFeature = Literal["voice_clone", "memory_feature"]
SubscriptionTier = Literal["free", "plus", "pro"]

logger = logging.getLogger(__name__)

_ACTIVE_LIKE_STATUSES = {"active", "renewed"}
_INACTIVE_STATUSES = {"expired", "failed", "on_hold", "pending"}
_SUPPORTED_SUBSCRIPTION_EVENTS = {
    "subscription.active",
    "subscription.updated",
    "subscription.on_hold",
    "subscription.renewed",
    "subscription.plan_changed",
    "subscription.cancelled",
    "subscription.failed",
    "subscription.expired",
}
_EVENT_STATUS_OVERRIDES = {
    "subscription.active": "active",
    "subscription.on_hold": "on_hold",
    "subscription.renewed": "active",
    "subscription.cancelled": "cancelled",
    "subscription.failed": "failed",
    "subscription.expired": "expired",
}
_DODO_API_BASE_URLS = {
    "live_mode": "https://live.dodopayments.com",
    "test_mode": "https://test.dodopayments.com",
}
_SUBSCRIPTION_STATUS_PRIORITY = {
    "active": 0,
    "renewed": 1,
    "on_hold": 2,
    "cancelled": 3,
    "pending": 4,
    "failed": 5,
    "expired": 6,
}
_DODO_HTTP_TIMEOUT_SECONDS = 15.0
_TIER_FEATURES: dict[SubscriptionTier, frozenset[SubscriptionFeature]] = {
    "free": frozenset(),
    "plus": frozenset({"voice_clone", "memory_feature"}),
    "pro": frozenset({"voice_clone", "memory_feature"}),
}
_TIER_RANK: dict[SubscriptionTier, int] = {"free": 0, "plus": 1, "pro": 2}


@dataclass(frozen=True)
class EffectiveEntitlementSummary:
    tier: SubscriptionTier
    effective_source: EffectiveEntitlementSource
    effective_expires_at: datetime | None
    active_pass: UserAccessPass | None


def _normalized_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_tier(value: str | None) -> SubscriptionTier:
    normalized = (value or "").strip().lower()
    if normalized == "plus":
        return "plus"
    if normalized == "pro":
        return "pro"
    return "free"


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso_datetime(value: object) -> datetime | None:
    normalized = _normalized_text(value)
    if normalized is None:
        return None
    try:
        return _coerce_utc(datetime.fromisoformat(normalized.replace("Z", "+00:00")))
    except ValueError:
        return None


def _parse_webhook_timestamp(value: str | None) -> datetime | None:
    normalized = _normalized_text(value)
    if normalized is None:
        return None
    try:
        return datetime.fromtimestamp(int(normalized), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _resolve_dodo_base_url(environment: str | None) -> str:
    normalized = (environment or "").strip().lower()
    return _DODO_API_BASE_URLS.get(normalized, _DODO_API_BASE_URLS["test_mode"])


def _extract_signature_candidates(signature_header: str) -> list[str]:
    candidates: list[str] = []
    for chunk in signature_header.split():
        normalized = chunk.strip().strip(",")
        if not normalized:
            continue
        if normalized.startswith("v1,"):
            candidates.append(normalized.split(",", 1)[1])
            continue
        if normalized.startswith("v1="):
            candidates.append(normalized.split("=", 1)[1])
            continue
        if normalized.startswith("v1:"):
            candidates.append(normalized.split(":", 1)[1])
            continue
        candidates.append(normalized)
    return [candidate for candidate in candidates if candidate]


def _extract_customer_id(data: Mapping[str, object]) -> str | None:
    customer_id = _normalized_text(data.get("customer_id"))
    if customer_id:
        return customer_id
    customer = data.get("customer")
    if isinstance(customer, dict):
        customer_payload = cast(dict[str, object], customer)
        return _normalized_text(customer_payload.get("customer_id"))
    return None


def _extract_items(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [cast(dict[str, object], item) for item in items if isinstance(item, dict)]


def _extract_customer_from_payload(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    customer_id = _normalized_text(payload.get("customer_id"))
    email = _normalized_text(payload.get("email"))
    if customer_id is None or email is None:
        return None
    return cast(dict[str, object], payload)


def _pick_latest_customer(customers: list[dict[str, object]]) -> dict[str, object] | None:
    if not customers:
        return None
    return max(
        customers,
        key=lambda item: (
            _parse_iso_datetime(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
        ),
    )


def _subscription_sort_key(item: Mapping[str, object]) -> tuple[int, datetime]:
    status = (_normalized_text(item.get("status")) or "").lower()
    timestamp = (
        _parse_iso_datetime(item.get("next_billing_date"))
        or _parse_iso_datetime(item.get("previous_billing_date"))
        or _parse_iso_datetime(item.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return (_SUBSCRIPTION_STATUS_PRIORITY.get(status, 999), timestamp)


def _pick_best_subscription(subscriptions: list[dict[str, object]]) -> dict[str, object] | None:
    if not subscriptions:
        return None
    return max(
        subscriptions,
        key=lambda item: (
            -_subscription_sort_key(item)[0],
            _subscription_sort_key(item)[1],
        ),
    )


def _tier_rank(tier: SubscriptionTier) -> int:
    return _TIER_RANK[tier]


class SubscriptionService:
    def __init__(
        self,
        user_repo: UserRepository,
        webhook_event_repo: SubscriptionWebhookEventRepository,
        user_access_pass_repo: UserAccessPassRepository,
        uow: UnitOfWork,
        settings_obj: Settings = settings,
    ) -> None:
        self._user_repo: UserRepository = user_repo
        self._webhook_event_repo: SubscriptionWebhookEventRepository = webhook_event_repo
        self._user_access_pass_repo: UserAccessPassRepository = user_access_pass_repo
        self._uow: UnitOfWork = uow
        self._settings: Settings = settings_obj

    @property
    def subscription_webhook_secret(self) -> str | None:
        return cast(
            str | None,
            getattr(
                self._settings,
                "DODO_PAYMENTS_SUBSCRIPTION_WEBHOOK_SECRET",
                getattr(self._settings, "DODO_PAYMENTS_WEBHOOK_SECRET", None),
            ),
        )

    @property
    def payment_webhook_secret(self) -> str | None:
        return cast(
            str | None,
            getattr(
                self._settings,
                "DODO_PAYMENTS_PAYMENT_WEBHOOK_SECRET",
                getattr(self._settings, "DODO_PAYMENTS_WEBHOOK_SECRET", None),
            ),
        )

    @property
    def paywall_enabled(self) -> bool:
        return bool(getattr(self._settings, "BILLING_PAYWALL_ENABLED", True))

    def map_product_id_to_tier(self, product_id: str | None) -> SubscriptionTier:
        normalized = _normalized_text(product_id)
        if normalized is None:
            return "free"

        plus_product_ids = set(self._settings.dodo_subscription_plus_product_ids)
        pro_product_ids = set(self._settings.dodo_subscription_pro_product_ids)

        for item in (
            self._settings.DODO_WECHAT_PLUS_30D_PRODUCT_ID,
            self._settings.DODO_WECHAT_PLUS_365D_PRODUCT_ID,
        ):
            if item:
                plus_product_ids.add(item)

        for item in (
            self._settings.DODO_WECHAT_PRO_30D_PRODUCT_ID,
            self._settings.DODO_WECHAT_PRO_365D_PRODUCT_ID,
        ):
            if item:
                pro_product_ids.add(item)

        if normalized in plus_product_ids:
            return "plus"
        if normalized in pro_product_ids:
            return "pro"
        return "free"

    async def reconcile_user_subscription(self, user: User, *, force: bool = False) -> bool:
        if not force and not self._should_reconcile(user):
            return False

        api_key = _normalized_text(self._settings.DODO_PAYMENTS_API_KEY)
        if api_key is None:
            logger.warning("Skip Dodo subscription reconciliation because API key is not configured")
            return False

        base_url = _resolve_dodo_base_url(self._settings.DODO_PAYMENTS_ENVIRONMENT)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                timeout=_DODO_HTTP_TIMEOUT_SECONDS,
            ) as client:
                customer = await self._resolve_dodo_customer(client, user)
                if customer is None:
                    logger.info(
                        "Dodo subscription reconciliation found no customer for user %s (%s)",
                        user.id,
                        user.email,
                    )
                    return False

                customer_id = _normalized_text(customer.get("customer_id"))
                if customer_id is None:
                    return False

                subscriptions = await self._list_dodo_subscriptions(client, customer_id)
        except httpx.HTTPError:
            logger.exception(
                "Dodo subscription reconciliation failed for user %s (%s)",
                user.id,
                user.email,
            )
            return False

        subscription = _pick_best_subscription(subscriptions)
        changed = False

        async with self._uow.transactional():
            if user.dodo_customer_id != customer_id:
                user.dodo_customer_id = customer_id
                changed = True

            next_product_id = (
                _normalized_text(subscription.get("product_id")) if subscription is not None else None
            )
            next_status = (
                _normalized_text(subscription.get("status")) if subscription is not None else None
            )
            next_period_end = (
                self._resolve_current_period_end(
                    _normalized_text(subscription.get("status")) or "",
                    subscription,
                    existing=None,
                )
                if subscription is not None
                else None
            )
            next_tier = self.map_product_id_to_tier(next_product_id)

            if user.subscription_product_id != next_product_id:
                user.subscription_product_id = next_product_id
                changed = True
            if user.subscription_status != next_status:
                user.subscription_status = next_status
                changed = True
            if user.subscription_current_period_end != next_period_end:
                user.subscription_current_period_end = next_period_end
                changed = True
            if user.subscription_tier != next_tier:
                user.subscription_tier = next_tier
                changed = True

        return changed

    async def get_effective_entitlement(self, user: User) -> EffectiveEntitlementSummary:
        recurring_tier = self._get_recurring_effective_tier(user)
        recurring_expires_at = self._get_recurring_effective_expires_at(user)
        active_pass = await self._user_access_pass_repo.get_highest_active_for_user(user.id)
        pass_tier = _normalize_tier(active_pass.tier if active_pass is not None else None)
        pass_expires_at = active_pass.ends_at if active_pass is not None else None

        recurring_rank = _tier_rank(recurring_tier)
        pass_rank = _tier_rank(pass_tier)

        if recurring_rank == 0 and pass_rank == 0:
            return EffectiveEntitlementSummary(
                tier="free",
                effective_source="none",
                effective_expires_at=None,
                active_pass=active_pass,
            )

        if recurring_rank > pass_rank:
            return EffectiveEntitlementSummary(
                tier=recurring_tier,
                effective_source="recurring_subscription",
                effective_expires_at=recurring_expires_at,
                active_pass=active_pass,
            )

        if pass_rank > recurring_rank:
            return EffectiveEntitlementSummary(
                tier=pass_tier,
                effective_source="one_time_pass",
                effective_expires_at=pass_expires_at,
                active_pass=active_pass,
            )

        if pass_expires_at is not None and (
            recurring_expires_at is None or pass_expires_at >= recurring_expires_at
        ):
            return EffectiveEntitlementSummary(
                tier=pass_tier,
                effective_source="one_time_pass",
                effective_expires_at=pass_expires_at,
                active_pass=active_pass,
            )

        return EffectiveEntitlementSummary(
            tier=recurring_tier,
            effective_source="recurring_subscription",
            effective_expires_at=recurring_expires_at,
            active_pass=active_pass,
        )

    async def get_effective_tier(self, user: User) -> SubscriptionTier:
        return (await self.get_effective_entitlement(user)).tier

    async def has_feature(self, user: User, feature: SubscriptionFeature) -> bool:
        if not self.paywall_enabled:
            return True
        summary = await self.get_effective_entitlement(user)
        return feature in _TIER_FEATURES[summary.tier]

    async def assert_feature(self, user: User, feature: SubscriptionFeature) -> None:
        if not self.paywall_enabled:
            return
        summary = await self.get_effective_entitlement(user)
        if feature in _TIER_FEATURES[summary.tier]:
            return

        raw_tier = _normalize_tier(user.subscription_tier)
        if raw_tier != "free":
            raise APIError(
                status_code=403,
                code="subscription_expired",
                message=(
                    "subscription unavailable: feature requires an active paid subscription; "
                    f"feature={feature}"
                ),
            )
        raise APIError(
            status_code=403,
            code="subscription_required",
            message=(
                "subscription required: feature requires a paid subscription tier; "
                f"feature={feature}"
            ),
        )

    def verify_dodo_webhook_signature(
        self,
        raw_body: bytes,
        *,
        webhook_id: str,
        webhook_signature: str,
        webhook_timestamp: str,
        webhook_secret: str | None = None,
    ) -> None:
        secret = _normalized_text(webhook_secret) or _normalized_text(
            self._settings.DODO_PAYMENTS_WEBHOOK_SECRET
        )
        if secret is None:
            raise APIError(
                status_code=500,
                code="internal_error",
                message="internal error: Dodo webhook secret is not configured",
            )

        try:
            payload = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: webhook payload must be valid UTF-8 JSON",
            ) from exc

        try:
            webhook_ts = int(webhook_timestamp)
        except (ValueError, TypeError):
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: webhook timestamp is not a valid Unix timestamp",
            )
        if abs(time.time() - webhook_ts) > 300:
            raise APIError(
                status_code=401,
                code="webhook_timestamp_expired",
                message="authorization failed: webhook timestamp is outside the 5-minute tolerance window",
            )

        signed_message = f"{webhook_id}.{webhook_timestamp}.{payload}".encode("utf-8")
        expected_signature = base64.b64encode(
            hmac.new(secret.encode("utf-8"), signed_message, hashlib.sha256).digest()
        ).decode("ascii")

        for candidate in _extract_signature_candidates(webhook_signature):
            if hmac.compare_digest(candidate, expected_signature):
                return

        raise APIError(
            status_code=401,
            code="webhook_invalid_signature",
            message="authorization failed: webhook signature is invalid",
        )

    async def build_entitlements_payload(
        self,
        user: User,
        user_settings: UserSettings | UserSettingsResponse,
    ) -> UserEntitlementsResponse:
        summary = await self.get_effective_entitlement(user)
        enabled_features = (
            frozenset({"voice_clone", "memory_feature"})
            if not self.paywall_enabled
            else _TIER_FEATURES[summary.tier]
        )
        return UserEntitlementsResponse(
            tier=summary.tier,
            subscription_status=user.subscription_status,
            subscription_product_id=user.subscription_product_id,
            current_period_end=user.subscription_current_period_end,
            effective_source=summary.effective_source,
            effective_expires_at=summary.effective_expires_at,
            active_pass=self._to_access_pass_response(summary.active_pass),
            features=UserEntitlementFeatures(
                voice_clone="voice_clone" in enabled_features,
                memory_feature="memory_feature" in enabled_features,
            ),
            settings=UserEntitlementSettings(memory_enabled=bool(user_settings.memory_enabled)),
        )

    def _should_reconcile(self, user: User) -> bool:
        if user.dodo_customer_id is None:
            return True
        if user.subscription_product_id is None:
            return True
        if user.subscription_status is None:
            return True
        return False

    def _get_recurring_effective_tier(self, user: User) -> SubscriptionTier:
        tier = _normalize_tier(user.subscription_tier)
        if tier == "free":
            return "free"

        status = (user.subscription_status or "").strip().lower()
        current_period_end = _coerce_utc(user.subscription_current_period_end)
        now = datetime.now(timezone.utc)

        if current_period_end is not None and current_period_end <= now:
            return "free"
        if status in _INACTIVE_STATUSES:
            return "free"
        if status == "cancelled" and current_period_end is not None and current_period_end <= now:
            return "free"
        if not status or status in _ACTIVE_LIKE_STATUSES or status == "cancelled":
            return tier
        return tier

    def _get_recurring_effective_expires_at(self, user: User) -> datetime | None:
        if self._get_recurring_effective_tier(user) == "free":
            return None
        return _coerce_utc(user.subscription_current_period_end)

    async def _resolve_dodo_customer(
        self,
        client: httpx.AsyncClient,
        user: User,
    ) -> dict[str, object] | None:
        if user.dodo_customer_id:
            customer = await self._retrieve_dodo_customer(client, user.dodo_customer_id)
            if customer is not None:
                return customer

        return await self._find_dodo_customer_by_email(client, user.email)

    async def _retrieve_dodo_customer(
        self,
        client: httpx.AsyncClient,
        customer_id: str,
    ) -> dict[str, object] | None:
        response = await client.get(f"/customers/{customer_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return _extract_customer_from_payload(response.json())

    async def _find_dodo_customer_by_email(
        self,
        client: httpx.AsyncClient,
        email: str,
    ) -> dict[str, object] | None:
        response = await client.get(
            "/customers",
            params={
                "email": email,
                "page_number": 0,
                "page_size": 100,
            },
        )
        response.raise_for_status()
        customers = _extract_items(response.json())
        exact_matches = [
            customer
            for customer in customers
            if (_normalized_text(customer.get("email")) or "").lower() == email.lower()
        ]
        return _pick_latest_customer(exact_matches)

    async def _list_dodo_subscriptions(
        self,
        client: httpx.AsyncClient,
        customer_id: str,
    ) -> list[dict[str, object]]:
        response = await client.get(
            "/subscriptions",
            params={
                "customer_id": customer_id,
                "page_number": 0,
                "page_size": 100,
            },
        )
        response.raise_for_status()
        return _extract_items(response.json())

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
        customer_id = _extract_customer_id(payload_data)
        subscription_id = _normalized_text(payload_data.get("subscription_id"))
        product_id = _normalized_text(payload_data.get("product_id"))

        async with self._uow.transactional():
            if await self._webhook_event_repo.get_by_webhook_id(webhook_id) is not None:
                return False

            event_record = await self._webhook_event_repo.create(
                webhook_id=webhook_id,
                event_type=event_type,
                webhook_timestamp=_parse_webhook_timestamp(webhook_timestamp),
                customer_id=customer_id,
                subscription_id=subscription_id,
                product_id=product_id,
                payload_json=event_payload,
            )

            processing_status = "ignored"
            if event_type in _SUPPORTED_SUBSCRIPTION_EVENTS and customer_id is not None:
                user = await self._user_repo.get_by_dodo_customer_id(customer_id)
                if user is not None:
                    resolved_product_id = product_id or user.subscription_product_id
                    resolved_status = _normalized_text(
                        payload_data.get("status")
                    ) or _EVENT_STATUS_OVERRIDES.get(event_type)
                    resolved_current_period_end = self._resolve_current_period_end(
                        event_type,
                        payload_data,
                        existing=user.subscription_current_period_end,
                    )

                    user.subscription_product_id = resolved_product_id
                    user.subscription_tier = self.map_product_id_to_tier(resolved_product_id)
                    user.subscription_status = resolved_status or user.subscription_status
                    user.subscription_current_period_end = resolved_current_period_end
                    processing_status = "processed"
                else:
                    processing_status = "ignored_unknown_customer"

            _ = await self._webhook_event_repo.mark_processed(
                event_record,
                processing_status=processing_status,
            )

        return True

    @staticmethod
    def _resolve_current_period_end(
        event_type: str,
        data: Mapping[str, object],
        *,
        existing: datetime | None,
    ) -> datetime | None:
        if event_type in {"subscription.expired", "subscription.failed"}:
            return None

        for field_name in (
            "current_period_end",
            "next_billing_date",
            "cancel_at_next_billing_date",
        ):
            parsed = _parse_iso_datetime(data.get(field_name))
            if parsed is not None:
                return parsed
        return existing

    @staticmethod
    def _to_access_pass_response(access_pass: UserAccessPass | None) -> UserAccessPassResponse | None:
        if access_pass is None:
            return None
        return UserAccessPassResponse(
            id=str(access_pass.id),
            channel=cast(Literal["wechat_pay"], access_pass.channel),
            product_id=access_pass.dodo_product_id,
            tier=cast(Literal["plus", "pro"], access_pass.tier),
            starts_at=access_pass.starts_at,
            ends_at=access_pass.ends_at,
            status=cast(Literal["active", "expired", "refunded", "revoked"], access_pass.status),
            source_order_id=str(access_pass.source_order_id),
        )
