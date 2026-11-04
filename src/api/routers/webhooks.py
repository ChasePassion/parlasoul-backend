from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Request

from src.api.response import success_response
from src.core.dependencies import get_subscription_service, get_wechat_payment_service
from src.core.exceptions import APIError
from src.services.subscription_service import SubscriptionService
from src.services.wechat_payment_service import WechatPaymentService

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


def _validate_webhook_headers(
    *,
    webhook_id: str | None,
    webhook_signature: str | None,
    webhook_timestamp: str | None,
) -> tuple[str, str, str]:
    if not webhook_id or not webhook_signature or not webhook_timestamp:
        raise APIError(
            status_code=400,
            code="invalid_param",
            message="invalid request: required webhook headers are missing",
        )
    return webhook_id, webhook_signature, webhook_timestamp


def _parse_webhook_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise APIError(
            status_code=400,
            code="invalid_param",
            message="invalid request: webhook payload is not valid JSON",
        ) from exc


@router.post("/dodo/subscriptions")
async def handle_dodo_subscription_webhook(
    request: Request,
    webhook_id: str | None = Header(default=None, alias="webhook-id"),
    webhook_signature: str | None = Header(default=None, alias="webhook-signature"),
    webhook_timestamp: str | None = Header(default=None, alias="webhook-timestamp"),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
):
    webhook_id, webhook_signature, webhook_timestamp = _validate_webhook_headers(
        webhook_id=webhook_id,
        webhook_signature=webhook_signature,
        webhook_timestamp=webhook_timestamp,
    )

    raw_body = await request.body()
    subscription_service.verify_dodo_webhook_signature(
        raw_body,
        webhook_id=webhook_id,
        webhook_signature=webhook_signature,
        webhook_timestamp=webhook_timestamp,
        webhook_secret=subscription_service.subscription_webhook_secret,
    )
    event = _parse_webhook_payload(raw_body)

    _ = await subscription_service.apply_webhook_event(
        event,
        webhook_id=webhook_id,
        webhook_timestamp=webhook_timestamp,
    )
    return success_response({"received": True}, status_code=200)


@router.post("/dodo/payments")
async def handle_dodo_payment_webhook(
    request: Request,
    webhook_id: str | None = Header(default=None, alias="webhook-id"),
    webhook_signature: str | None = Header(default=None, alias="webhook-signature"),
    webhook_timestamp: str | None = Header(default=None, alias="webhook-timestamp"),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
    payment_service: WechatPaymentService = Depends(get_wechat_payment_service),
):
    webhook_id, webhook_signature, webhook_timestamp = _validate_webhook_headers(
        webhook_id=webhook_id,
        webhook_signature=webhook_signature,
        webhook_timestamp=webhook_timestamp,
    )

    raw_body = await request.body()
    subscription_service.verify_dodo_webhook_signature(
        raw_body,
        webhook_id=webhook_id,
        webhook_signature=webhook_signature,
        webhook_timestamp=webhook_timestamp,
        webhook_secret=subscription_service.payment_webhook_secret,
    )
    event = _parse_webhook_payload(raw_body)

    _ = await payment_service.apply_webhook_event(
        event,
        webhook_id=webhook_id,
        webhook_timestamp=webhook_timestamp,
    )
    return success_response({"received": True}, status_code=200)
