from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import APIError
from src.db.models import SubscriptionWebhookEvent, User, UserSettings
from src.repositories.subscription_webhook_event_repository import (
    SubscriptionWebhookEventRepository,
)
from src.repositories.user_access_pass_repository import UserAccessPassRepository
from src.repositories.user_repository import UserRepository
from src.services.subscription_service import SubscriptionService


class _StubUow:
    @asynccontextmanager
    async def transactional(self):
        yield


def _build_user(
    *,
    email: str = "user@example.com",
    dodo_customer_id: str | None = None,
    subscription_tier: str = "free",
    subscription_status: str | None = None,
    subscription_product_id: str | None = None,
    subscription_current_period_end: datetime | None = None,
) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        dodo_customer_id=dodo_customer_id,
        subscription_tier=subscription_tier,
        subscription_status=subscription_status,
        subscription_product_id=subscription_product_id,
        subscription_current_period_end=subscription_current_period_end,
    )


def _build_settings(memory_enabled: bool = True) -> UserSettings:
    return UserSettings(
        user_id=uuid.uuid4(),
        message_font_size=16,
        display_mode="concise",
        memory_enabled=memory_enabled,
        reply_card_enabled=True,
        mixed_input_auto_translate_enabled=True,
        auto_read_aloud_enabled=True,
        preferred_expression_bias_enabled=True,
        updated_at=datetime.now(UTC),
    )


def _build_dodo_settings(
    *,
    plus_product_ids: list[str] | None = None,
    pro_product_ids: list[str] | None = None,
    webhook_secret: str = "test-dodo-secret",
    api_key: str | None = "dodo_test_key",
) -> SimpleNamespace:
    return SimpleNamespace(
        BILLING_PAYWALL_ENABLED=True,
        dodo_subscription_plus_product_ids=plus_product_ids or [],
        dodo_subscription_pro_product_ids=pro_product_ids or [],
        DODO_WECHAT_PLUS_30D_PRODUCT_ID=None,
        DODO_WECHAT_PLUS_365D_PRODUCT_ID=None,
        DODO_WECHAT_PRO_30D_PRODUCT_ID=None,
        DODO_WECHAT_PRO_365D_PRODUCT_ID=None,
        DODO_PAYMENTS_WEBHOOK_SECRET=webhook_secret,
        DODO_PAYMENTS_API_KEY=api_key,
        DODO_PAYMENTS_ENVIRONMENT="test_mode",
    )


def _build_service() -> tuple[
    SubscriptionService,
    UserRepository,
    SubscriptionWebhookEventRepository,
    UserAccessPassRepository,
]:
    user_repo = UserRepository.__new__(UserRepository)
    event_repo = SubscriptionWebhookEventRepository.__new__(SubscriptionWebhookEventRepository)
    pass_repo = UserAccessPassRepository.__new__(UserAccessPassRepository)
    pass_repo.get_highest_active_for_user = AsyncMock(return_value=None)
    return (
        SubscriptionService(
            user_repo,
            event_repo,
            pass_repo,
            _StubUow(),
            settings_obj=_build_dodo_settings(),
        ),
        user_repo,
        event_repo,
        pass_repo,
    )


@pytest.mark.asyncio
async def test_build_entitlements_payload_uses_effective_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _ = _build_service()
    service._settings = _build_dodo_settings(plus_product_ids=["prod_plus"])
    user = _build_user(
        subscription_tier="plus",
        subscription_status="active",
        subscription_product_id="prod_plus",
        subscription_current_period_end=datetime.now(UTC) + timedelta(days=30),
    )
    settings = _build_settings(memory_enabled=False)

    payload = await service.build_entitlements_payload(user, settings)

    assert payload.tier == "plus"
    assert payload.subscription_status == "active"
    assert payload.subscription_product_id == "prod_plus"
    assert payload.features.voice_clone is True
    assert payload.features.memory_feature is True
    assert payload.settings.memory_enabled is False


@pytest.mark.asyncio
async def test_paywall_disabled_unlocks_feature_flags_for_free_user() -> None:
    service, _, _, _ = _build_service()
    service._settings.BILLING_PAYWALL_ENABLED = False
    user = _build_user(subscription_tier="free")
    settings = _build_settings(memory_enabled=True)

    payload = await service.build_entitlements_payload(user, settings)

    assert payload.tier == "free"
    assert payload.features.voice_clone is True
    assert payload.features.memory_feature is True
    assert await service.has_feature(user, "voice_clone") is True
    await service.assert_feature(user, "memory_feature")


def test_map_product_id_to_tier_supports_multiple_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _ = _build_service()
    service._settings = _build_dodo_settings(
        plus_product_ids=["prod_plus_monthly", "prod_plus_yearly"],
        pro_product_ids=["prod_pro_monthly", "prod_pro_yearly"],
    )

    assert service.map_product_id_to_tier("prod_plus_monthly") == "plus"
    assert service.map_product_id_to_tier("prod_plus_yearly") == "plus"
    assert service.map_product_id_to_tier("prod_pro_monthly") == "pro"
    assert service.map_product_id_to_tier("prod_pro_yearly") == "pro"
    assert service.map_product_id_to_tier("prod_unknown") == "free"


@pytest.mark.asyncio
async def test_assert_feature_raises_subscription_expired_for_paid_but_inactive_user() -> None:
    service, _, _, _ = _build_service()
    user = _build_user(
        subscription_tier="plus",
        subscription_status="expired",
        subscription_current_period_end=datetime.now(UTC) - timedelta(days=1),
    )

    with pytest.raises(APIError) as exc:
        await service.assert_feature(user, "voice_clone")

    assert exc.value.code == "subscription_expired"


@pytest.mark.asyncio
async def test_apply_webhook_event_updates_user_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    service, user_repo, event_repo, _ = _build_service()
    service._settings = _build_dodo_settings(plus_product_ids=["prod_plus"])
    user = _build_user(dodo_customer_id="cust_123")
    event_record = SubscriptionWebhookEvent(
        webhook_id="wh_123",
        event_type="subscription.active",
        payload_json={},
    )

    user_repo.get_by_dodo_customer_id = AsyncMock(return_value=user)
    event_repo.get_by_webhook_id = AsyncMock(side_effect=[None, None])
    event_repo.create = AsyncMock(return_value=event_record)
    event_repo.mark_processed = AsyncMock(return_value=event_record)

    result = await service.apply_webhook_event(
        {
            "type": "subscription.active",
            "data": {
                "subscription_id": "sub_123",
                "product_id": "prod_plus",
                "status": "active",
                "next_billing_date": "2026-05-01T00:00:00Z",
                "customer": {"customer_id": "cust_123"},
            },
        },
        webhook_id="wh_123",
        webhook_timestamp="1710000000",
    )

    assert result is True
    assert user.subscription_tier == "plus"
    assert user.subscription_status == "active"
    assert user.subscription_product_id == "prod_plus"
    assert user.subscription_current_period_end == datetime(2026, 5, 1, tzinfo=UTC)
    event_repo.create.assert_awaited_once()
    event_repo.mark_processed.assert_awaited_once_with(
        event_record,
        processing_status="processed",
    )


@pytest.mark.asyncio
async def test_apply_webhook_event_short_circuits_duplicate() -> None:
    service, user_repo, event_repo, _ = _build_service()
    event_repo.get_by_webhook_id = AsyncMock(
        return_value=SubscriptionWebhookEvent(
            webhook_id="wh_dup",
            event_type="subscription.active",
            payload_json={},
        )
    )
    event_repo.create = AsyncMock()
    user_repo.get_by_dodo_customer_id = AsyncMock()

    result = await service.apply_webhook_event(
        {"type": "subscription.active", "data": {"customer": {"customer_id": "cust_dup"}}},
        webhook_id="wh_dup",
        webhook_timestamp="1710000000",
    )

    assert result is False
    event_repo.create.assert_not_called()
    user_repo.get_by_dodo_customer_id.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_user_subscription_backfills_customer_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _ = _build_service()
    service._settings = _build_dodo_settings(
        plus_product_ids=["prod_plus_monthly", "prod_plus_yearly"],
        api_key="dodo_test_key",
    )
    user = _build_user(email="user@example.com")
    service._resolve_dodo_customer = AsyncMock(
        return_value={
            "customer_id": "cust_test_123",
            "email": "user@example.com",
            "created_at": "2026-04-01T00:00:00Z",
        }
    )
    service._list_dodo_subscriptions = AsyncMock(
        return_value=[
            {
                "subscription_id": "sub_123",
                "product_id": "prod_plus_monthly",
                "status": "active",
                "next_billing_date": "2026-05-01T00:00:00Z",
                "created_at": "2026-04-01T00:00:00Z",
            }
        ]
    )

    changed = await service.reconcile_user_subscription(user)

    assert changed is True
    assert user.dodo_customer_id == "cust_test_123"
    assert user.subscription_tier == "plus"
    assert user.subscription_status == "active"
    assert user.subscription_product_id == "prod_plus_monthly"
    assert user.subscription_current_period_end == datetime(2026, 5, 1, tzinfo=UTC)
