from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import APIError
from src.db.models import PaymentOrder, User
from src.services.subscription_service import EffectiveEntitlementSummary
from src.services import wechat_payment_service as wps
from src.services.wechat_payment_service import WechatPaymentService


class _StubUow:
    @asynccontextmanager
    async def transactional(self):
        yield


def _build_settings() -> SimpleNamespace:
    return SimpleNamespace(
        BILLING_CHECKOUT_ENABLED=True,
        DODO_WECHAT_PLUS_30D_PRODUCT_ID="pdt_plus_30",
        DODO_WECHAT_PRO_30D_PRODUCT_ID="pdt_pro_30",
        DODO_WECHAT_PLUS_365D_PRODUCT_ID="pdt_plus_365",
        DODO_WECHAT_PRO_365D_PRODUCT_ID="pdt_pro_365",
        DODO_PAYMENTS_API_KEY="dodo_test_key",
        DODO_PAYMENTS_ENVIRONMENT="test_mode",
        DODO_PAYMENTS_WEB_APP_BASE_URL="http://localhost:3001",
    )


def _build_user() -> User:
    return User(id=uuid.uuid4(), email="buyer@example.com", username="buyer")


def _build_order(user: User) -> PaymentOrder:
    return PaymentOrder(
        id=uuid.uuid4(),
        user_id=user.id,
        dodo_product_id="pdt_plus_30",
        tier="plus",
        duration_days=30,
        billing_currency="CNY",
        status="checkout_created",
    )


def _build_service(**overrides) -> WechatPaymentService:
    order_repo = overrides.get(
        "order_repo",
        SimpleNamespace(
            create=AsyncMock(side_effect=lambda order: order),
            apply_update=AsyncMock(
                side_effect=lambda order, update_data: [
                    setattr(order, field_name, value)
                    for field_name, value in update_data.items()
                ]
                or order
            ),
            get_by_id=AsyncMock(return_value=None),
            get_by_id_for_user=AsyncMock(return_value=None),
            get_by_payment_id=AsyncMock(return_value=None),
            get_by_checkout_session_id=AsyncMock(return_value=None),
            list_by_user=AsyncMock(return_value=[]),
        ),
    )
    webhook_event_repo = overrides.get(
        "webhook_event_repo",
        SimpleNamespace(
            get_by_webhook_id=AsyncMock(return_value=None),
            create=AsyncMock(return_value=SimpleNamespace()),
            mark_processed=AsyncMock(),
        ),
    )
    user_access_pass_repo = overrides.get(
        "user_access_pass_repo",
        SimpleNamespace(
            upsert_active_pass=AsyncMock(),
            get_by_source_order_id=AsyncMock(return_value=None),
        ),
    )
    user_repo = overrides.get(
        "user_repo",
        SimpleNamespace(get_by_id=AsyncMock(return_value=None)),
    )
    subscription_service = overrides.get(
        "subscription_service",
        SimpleNamespace(
            get_effective_entitlement=AsyncMock(
                return_value=EffectiveEntitlementSummary(
                    tier="free",
                    effective_source="none",
                    effective_expires_at=None,
                    active_pass=None,
                )
            )
        ),
    )
    return WechatPaymentService(
        order_repo=order_repo,
        webhook_event_repo=webhook_event_repo,
        user_access_pass_repo=user_access_pass_repo,
        user_repo=user_repo,
        subscription_service=subscription_service,
        uow=_StubUow(),
        settings_obj=overrides.get("settings_obj", _build_settings()),
    )


def _build_service_with_mocked_cache(**overrides) -> WechatPaymentService:
    service = _build_service(**overrides)
    service._refresh_product_cache = AsyncMock()  # type: ignore[method-assign]
    wps._product_cache = {
        "pdt_plus_30": {"price_minor": 1000, "currency": "USD", "name": "ParlaSoul Plus 30 Days"},
        "pdt_pro_30": {"price_minor": 2000, "currency": "USD", "name": "ParlaSoul Pro 30 Days"},
        "pdt_plus_365": {"price_minor": 10000, "currency": "USD", "name": "ParlaSoul Plus 365 Days"},
        "pdt_pro_365": {"price_minor": 20000, "currency": "USD", "name": "ParlaSoul Pro 365 Days"},
    }
    return service


@pytest.mark.asyncio
async def test_create_checkout_session_returns_redirect_payload() -> None:
    user = _build_user()
    service = _build_service_with_mocked_cache()
    service._create_dodo_checkout = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "session_id": "cs_123",
            "checkout_url": "https://checkout.example.com/cs_123",
        }
    )

    result = await service.create_checkout_session(
        current_user=user,
        product_id="pdt_plus_30",
    )

    assert result.checkout_session_id == "cs_123"
    assert result.checkout_url == "https://checkout.example.com/cs_123"
    assert result.product_id == "pdt_plus_30"
    assert result.tier == "plus"


@pytest.mark.asyncio
async def test_create_checkout_session_rejects_when_checkout_disabled() -> None:
    user = _build_user()
    settings = _build_settings()
    settings.BILLING_CHECKOUT_ENABLED = False
    service = _build_service_with_mocked_cache(settings_obj=settings)
    service._create_dodo_checkout = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(APIError) as exc:
        await service.create_checkout_session(
            current_user=user,
            product_id="pdt_plus_30",
        )

    assert exc.value.status_code == 409
    assert exc.value.code == "resource_conflict"
    service._create_dodo_checkout.assert_not_called()


@pytest.mark.asyncio
async def test_create_checkout_session_rejects_when_effective_tier_already_covers_request() -> None:
    user = _build_user()
    subscription_service = SimpleNamespace(
        get_effective_entitlement=AsyncMock(
            return_value=EffectiveEntitlementSummary(
                tier="pro",
                effective_source="recurring_subscription",
                effective_expires_at=datetime.now(UTC),
                active_pass=None,
            )
        )
    )
    service = _build_service_with_mocked_cache(subscription_service=subscription_service)

    with pytest.raises(APIError) as exc:
        await service.create_checkout_session(current_user=user, product_id="pdt_plus_30")

    assert exc.value.code == "resource_conflict"


@pytest.mark.asyncio
async def test_apply_payment_succeeded_webhook_updates_order_and_activates_pass() -> None:
    user = _build_user()
    order = _build_order(user)
    event_record = SimpleNamespace()
    order_repo = SimpleNamespace(
        create=AsyncMock(),
        apply_update=AsyncMock(
            side_effect=lambda target, update_data: [
                setattr(target, field_name, value)
                for field_name, value in update_data.items()
            ]
            or target
        ),
        get_by_id=AsyncMock(return_value=order),
        get_by_id_for_user=AsyncMock(return_value=order),
        get_by_payment_id=AsyncMock(return_value=None),
        get_by_checkout_session_id=AsyncMock(return_value=None),
        list_by_user=AsyncMock(return_value=[]),
    )
    webhook_event_repo = SimpleNamespace(
        get_by_webhook_id=AsyncMock(side_effect=[None, None]),
        create=AsyncMock(return_value=event_record),
        mark_processed=AsyncMock(return_value=event_record),
    )
    user_access_pass_repo = SimpleNamespace(
        upsert_active_pass=AsyncMock(),
        get_by_source_order_id=AsyncMock(return_value=None),
    )
    user_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=user))
    service = _build_service_with_mocked_cache(
        order_repo=order_repo,
        webhook_event_repo=webhook_event_repo,
        user_access_pass_repo=user_access_pass_repo,
        user_repo=user_repo,
    )

    result = await service.apply_webhook_event(
        {
            "type": "payment.succeeded",
            "timestamp": "2026-04-12T12:00:00Z",
            "data": {
                "payment_id": "pay_123",
                "checkout_session_id": "cs_123",
                "total_amount": 5000,
                "currency": "CNY",
                "settlement_amount": 5000,
                "settlement_currency": "CNY",
                "created_at": "2026-04-12T12:00:00Z",
                "customer": {"customer_id": "cust_123"},
                "metadata": {
                    "order_id": str(order.id),
                    "product_id": order.dodo_product_id,
                },
            },
        },
        webhook_id="wh_123",
        webhook_timestamp="1710000000",
    )

    assert result is True
    assert order.status == "succeeded"
    assert order.dodo_payment_id == "pay_123"
    assert order.dodo_customer_id == "cust_123"
    user_access_pass_repo.upsert_active_pass.assert_awaited_once()
    webhook_event_repo.mark_processed.assert_awaited_once_with(
        event_record,
        processing_status="processed",
    )


@pytest.mark.asyncio
async def test_list_products_raises_when_cache_is_empty() -> None:
    service = _build_service()
    service._refresh_product_cache = AsyncMock()  # type: ignore[method-assign]
    wps._product_cache.clear()

    with pytest.raises(APIError) as exc:
        await service.list_products()

    assert exc.value.status_code == 502
    assert exc.value.code == "external_error"


@pytest.mark.asyncio
async def test_create_checkout_session_raises_when_cache_is_empty() -> None:
    user = _build_user()
    service = _build_service()
    service._refresh_product_cache = AsyncMock()  # type: ignore[method-assign]
    wps._product_cache.clear()

    with pytest.raises(APIError) as exc:
        await service.create_checkout_session(
            current_user=user,
            product_id="pdt_plus_30",
        )

    assert exc.value.status_code == 502
    assert exc.value.code == "external_error"
