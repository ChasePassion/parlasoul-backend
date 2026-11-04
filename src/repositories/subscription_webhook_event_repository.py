from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import SubscriptionWebhookEvent


class SubscriptionWebhookEventRepository:
    def __init__(self, db: AsyncSession):
        self._db: AsyncSession = db

    async def get_by_webhook_id(self, webhook_id: str) -> SubscriptionWebhookEvent | None:
        stmt = select(SubscriptionWebhookEvent).where(
            SubscriptionWebhookEvent.webhook_id == webhook_id
        )
        result = await self._db.exec(stmt)
        return result.first()

    async def create(
        self,
        *,
        webhook_id: str,
        event_type: str,
        webhook_timestamp: datetime | None,
        customer_id: str | None,
        subscription_id: str | None,
        product_id: str | None,
        payload_json: Mapping[str, object],
        processing_status: str = "received",
    ) -> SubscriptionWebhookEvent:
        event = SubscriptionWebhookEvent(
            webhook_id=webhook_id,
            event_type=event_type,
            webhook_timestamp=webhook_timestamp,
            customer_id=customer_id,
            subscription_id=subscription_id,
            product_id=product_id,
            payload_json=dict(payload_json),
            processing_status=processing_status,
        )
        self._db.add(event)
        await self._db.flush()
        return event

    async def mark_processed(
        self,
        event: SubscriptionWebhookEvent,
        *,
        processing_status: str,
    ) -> SubscriptionWebhookEvent:
        event.processing_status = processing_status
        event.processed_at = datetime.now(timezone.utc)
        await self._db.flush()
        return event
