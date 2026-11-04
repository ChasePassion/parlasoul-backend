from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import payments
from src.core.dependencies import get_wechat_payment_service
from src.db.models import User
from src.schemas.payment import (
    CreateWechatCheckoutSessionResponse,
    PaymentOrderResponse,
    WechatPaymentProductListResponse,
    WechatPaymentProductResponse,
)


class _StubPaymentService:
    def __init__(self) -> None:
        self.list_products = AsyncMock()
        self.create_checkout_session = AsyncMock()
        self.get_order_for_user = AsyncMock()
        self.list_orders_for_user = AsyncMock()


def _build_user() -> User:
    return User(id=uuid.uuid4(), email="buyer@example.com")


def _create_app(service: _StubPaymentService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(payments.router)

    async def _override_current_user() -> User:
        return _build_user()

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_wechat_payment_service] = lambda: service
    return app


def test_list_wechat_products_route_delegates_to_service() -> None:
    service = _StubPaymentService()
    service.list_products.return_value = WechatPaymentProductListResponse(
        items=[
            WechatPaymentProductResponse(
                product_id="pdt_plus",
                name="ParlaSoul Plus 30 Days",
                tier="plus",
                duration_days=30,
                base_price_minor=1000,
                base_price_currency="USD",
                base_price_display="$10.00",
                billing_currency="CNY",
                channel="wechat_pay",
                price_note="微信支付将以人民币结算，最终金额以结账页为准",
            )
        ]
    )
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.get("/v1/payments/wechat/products")

    assert resp.status_code == 200
    assert resp.json()["data"]["items"][0]["product_id"] == "pdt_plus"


def test_create_wechat_checkout_session_route_delegates_to_service() -> None:
    service = _StubPaymentService()
    service.create_checkout_session.return_value = CreateWechatCheckoutSessionResponse(
        order_id=str(uuid.uuid4()),
        checkout_url="https://checkout.example.com",
        checkout_session_id="cs_123",
        product_id="pdt_plus",
        tier="plus",
        duration_days=30,
        billing_currency="CNY",
        channel="wechat_pay",
    )
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/payments/wechat/checkout-session",
            json={"product_id": "pdt_plus"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["checkout_session_id"] == "cs_123"
    service.create_checkout_session.assert_awaited_once()


def test_get_payment_order_route_delegates_to_service() -> None:
    service = _StubPaymentService()
    service.get_order_for_user.return_value = PaymentOrderResponse(
        id=str(uuid.uuid4()),
        status="pending",
        product_id="pdt_plus",
        tier="plus",
        duration_days=30,
        channel="wechat_pay",
        billing_currency="CNY",
        paid_at=None,
        refunded_at=None,
        created_at=datetime.now(UTC),
        charged_total_minor=None,
        charged_currency=None,
        settlement_total_minor=None,
        settlement_currency=None,
    )
    app = _create_app(service)
    order_id = str(uuid.uuid4())

    with TestClient(app) as client:
        resp = client.get(f"/v1/payments/orders/{order_id}")

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "pending"
    service.get_order_for_user.assert_awaited_once()


def test_list_payment_orders_route_delegates_to_service() -> None:
    service = _StubPaymentService()
    service.list_orders_for_user.return_value = [
        PaymentOrderResponse(
            id=str(uuid.uuid4()),
            status="succeeded",
            product_id="pdt_plus",
            tier="plus",
            duration_days=30,
            channel="wechat_pay",
            billing_currency="CNY",
            paid_at=datetime.now(UTC),
            refunded_at=None,
            created_at=datetime.now(UTC),
            charged_total_minor=5000,
            charged_currency="CNY",
            settlement_total_minor=5000,
            settlement_currency="CNY",
        )
    ]
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.get("/v1/payments/orders?channel=wechat_pay")

    assert resp.status_code == 200
    assert resp.json()["data"][0]["status"] == "succeeded"
    service.list_orders_for_user.assert_awaited_once()
