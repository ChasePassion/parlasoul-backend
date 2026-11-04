from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlmodel import SQLModel

PaymentTier = Literal["plus", "pro"]
PaymentChannel = Literal["wechat_pay"]
PaymentOrderStatus = Literal[
    "created",
    "checkout_created",
    "pending",
    "succeeded",
    "failed",
    "cancelled",
    "refunded",
    "expired",
]
UserAccessPassStatus = Literal["active", "expired", "refunded", "revoked"]


class UserAccessPassResponse(SQLModel):
    id: str
    channel: PaymentChannel
    product_id: str
    tier: PaymentTier
    starts_at: datetime
    ends_at: datetime
    status: UserAccessPassStatus
    source_order_id: str


class WechatPaymentProductResponse(SQLModel):
    product_id: str
    name: str
    tier: PaymentTier
    duration_days: int
    base_price_minor: int
    base_price_currency: str
    base_price_display: str
    billing_currency: str
    channel: PaymentChannel
    price_note: str


class WechatPaymentProductListResponse(SQLModel):
    items: list[WechatPaymentProductResponse]


class CreateWechatCheckoutSessionRequest(SQLModel):
    product_id: str


class CreateWechatCheckoutSessionResponse(SQLModel):
    order_id: str
    checkout_url: str
    checkout_session_id: str
    product_id: str
    tier: PaymentTier
    duration_days: int
    billing_currency: str
    channel: PaymentChannel


class PaymentOrderResponse(SQLModel):
    id: str
    status: PaymentOrderStatus
    product_id: str
    tier: PaymentTier
    duration_days: int
    channel: PaymentChannel
    billing_currency: str
    paid_at: datetime | None = None
    refunded_at: datetime | None = None
    created_at: datetime
    charged_total_minor: int | None = None
    charged_currency: str | None = None
    settlement_total_minor: int | None = None
    settlement_currency: str | None = None
