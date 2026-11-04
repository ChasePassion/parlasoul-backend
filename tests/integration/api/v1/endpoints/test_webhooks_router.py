from __future__ import annotations

import base64
import hashlib
import hmac
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.exceptions import register_exception_handlers
from src.api.routers import webhooks
from src.core.dependencies import get_subscription_service, get_wechat_payment_service
from src.repositories.subscription_webhook_event_repository import (
    SubscriptionWebhookEventRepository,
)
from src.repositories.user_access_pass_repository import UserAccessPassRepository
from src.repositories.user_repository import UserRepository
from src.services.subscription_service import SubscriptionService


class _StubUow:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _build_service() -> SubscriptionService:
    user_repo = UserRepository.__new__(UserRepository)
    event_repo = SubscriptionWebhookEventRepository.__new__(SubscriptionWebhookEventRepository)
    pass_repo = UserAccessPassRepository.__new__(UserAccessPassRepository)
    pass_repo.get_highest_active_for_user = AsyncMock(return_value=None)
    service = SubscriptionService(
        user_repo,
        event_repo,
        pass_repo,
        type("StubUow", (), {"transactional": lambda self: _StubUow()})(),
    )
    service._settings = SimpleNamespace(
        DODO_PAYMENTS_WEBHOOK_SECRET="test-dodo-secret",
        DODO_PAYMENTS_SUBSCRIPTION_WEBHOOK_SECRET="test-dodo-secret",
        DODO_PAYMENTS_PAYMENT_WEBHOOK_SECRET="test-payment-secret",
    )
    service.apply_webhook_event = AsyncMock()
    return service


class _StubPaymentService:
    def __init__(self) -> None:
        self.apply_webhook_event = AsyncMock()


def _sign(secret: str, webhook_id: str, webhook_timestamp: str, payload: str) -> str:
    signed = f"{webhook_id}.{webhook_timestamp}.{payload}".encode()
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    return f"v1,{base64.b64encode(digest).decode('ascii')}"


def _create_app(
    subscription_service: SubscriptionService,
    payment_service: _StubPaymentService | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(webhooks.router)
    app.dependency_overrides[get_subscription_service] = lambda: subscription_service
    app.dependency_overrides[get_wechat_payment_service] = lambda: (
        payment_service or _StubPaymentService()
    )
    return app


def test_dodo_subscription_webhook_accepts_valid_signature(
    monkeypatch,
) -> None:
    secret = "test-dodo-secret"
    webhook_id = "wh_123"
    webhook_timestamp = str(int(time.time()))
    payload = '{"type":"subscription.active","data":{"customer":{"customer_id":"cust_123"}}}'
    service = _build_service()
    app = _create_app(service)
    service._settings.DODO_PAYMENTS_SUBSCRIPTION_WEBHOOK_SECRET = secret

    with TestClient(app) as client:
        resp = client.post(
            "/v1/webhooks/dodo/subscriptions",
            content=payload,
            headers={
                "content-type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": webhook_timestamp,
                "webhook-signature": _sign(secret, webhook_id, webhook_timestamp, payload),
            },
        )

    assert resp.status_code == 200
    assert resp.json()["data"] == {"received": True}
    service.apply_webhook_event.assert_awaited_once()


def test_dodo_subscription_webhook_rejects_invalid_signature(
    monkeypatch,
) -> None:
    service = _build_service()
    app = _create_app(service)
    service._settings.DODO_PAYMENTS_SUBSCRIPTION_WEBHOOK_SECRET = "test-dodo-secret"

    with TestClient(app) as client:
        resp = client.post(
            "/v1/webhooks/dodo/subscriptions",
            content='{"type":"subscription.active","data":{}}',
            headers={
                "content-type": "application/json",
                "webhook-id": "wh_bad",
                "webhook-timestamp": str(int(time.time())),
                "webhook-signature": "v1,not-valid",
            },
        )

    assert resp.status_code == 401
    assert resp.json()["code"] == "webhook_invalid_signature"
    service.apply_webhook_event.assert_not_awaited()


def test_dodo_payment_webhook_accepts_valid_signature(
    monkeypatch,
) -> None:
    secret = "test-dodo-secret"
    webhook_id = "wh_pay_123"
    webhook_timestamp = str(int(time.time()))
    payload = '{"type":"payment.succeeded","data":{"payment_id":"pay_123","metadata":{"order_id":"11111111-1111-1111-1111-111111111111"}}}'
    subscription_service = _build_service()
    payment_service = _StubPaymentService()
    app = _create_app(subscription_service, payment_service)
    subscription_service._settings.DODO_PAYMENTS_SUBSCRIPTION_WEBHOOK_SECRET = "different-subscription-secret"
    subscription_service._settings.DODO_PAYMENTS_PAYMENT_WEBHOOK_SECRET = secret

    with TestClient(app) as client:
        resp = client.post(
            "/v1/webhooks/dodo/payments",
            content=payload,
            headers={
                "content-type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": webhook_timestamp,
                "webhook-signature": _sign(secret, webhook_id, webhook_timestamp, payload),
            },
        )

    assert resp.status_code == 200
    assert resp.json()["data"] == {"received": True}
    payment_service.apply_webhook_event.assert_awaited_once()
