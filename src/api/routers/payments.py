from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from src.api import deps
from src.api.response import success_response
from src.core.dependencies import get_wechat_payment_service
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.payment import (
    CreateWechatCheckoutSessionRequest,
    CreateWechatCheckoutSessionResponse,
    PaymentOrderResponse,
    WechatPaymentProductListResponse,
)
from src.services.wechat_payment_service import WechatPaymentService

router = APIRouter(prefix="/v1/payments", tags=["payments"])


@router.get(
    "/wechat/products",
    response_model=SuccessEnvelope[WechatPaymentProductListResponse],
)
async def list_wechat_products(
    payment_service: WechatPaymentService = Depends(get_wechat_payment_service),
) -> JSONResponse:
    data = await payment_service.list_products()
    return success_response(data, status_code=200)


@router.post(
    "/wechat/checkout-session",
    response_model=SuccessEnvelope[CreateWechatCheckoutSessionResponse],
)
async def create_wechat_checkout_session(
    request: CreateWechatCheckoutSessionRequest,
    current_user: User = Depends(deps.get_current_user),
    payment_service: WechatPaymentService = Depends(get_wechat_payment_service),
) -> JSONResponse:
    data = await payment_service.create_checkout_session(
        current_user=current_user,
        product_id=request.product_id,
    )
    return success_response(data, status_code=200)


@router.get("/orders/{order_id}", response_model=SuccessEnvelope[PaymentOrderResponse])
async def get_payment_order(
    order_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    payment_service: WechatPaymentService = Depends(get_wechat_payment_service),
) -> JSONResponse:
    data = await payment_service.get_order_for_user(
        order_id=order_id,
        current_user=current_user,
    )
    return success_response(data, status_code=200)


@router.get("/orders", response_model=SuccessEnvelope[list[PaymentOrderResponse]])
async def list_payment_orders(
    channel: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(deps.get_current_user),
    payment_service: WechatPaymentService = Depends(get_wechat_payment_service),
) -> JSONResponse:
    data = await payment_service.list_orders_for_user(
        current_user=current_user,
        channel=channel,
        skip=skip,
        limit=limit,
    )
    return success_response(data, status_code=200)
