from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import PaymentOrder


class PaymentOrderRepository:
    def __init__(self, db: AsyncSession):
        self._db: AsyncSession = db

    async def create(self, order: PaymentOrder) -> PaymentOrder:
        self._db.add(order)
        await self._db.flush()
        return order

    async def get_by_id(self, order_id: uuid.UUID) -> PaymentOrder | None:
        stmt = select(PaymentOrder).where(PaymentOrder.id == order_id)
        result = await self._db.exec(stmt)
        return result.first()

    async def get_by_id_for_user(
        self,
        *,
        order_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> PaymentOrder | None:
        stmt = select(PaymentOrder).where(
            PaymentOrder.id == order_id,
            PaymentOrder.user_id == user_id,
        )
        result = await self._db.exec(stmt)
        return result.first()

    async def get_by_payment_id(self, payment_id: str) -> PaymentOrder | None:
        stmt = select(PaymentOrder).where(PaymentOrder.dodo_payment_id == payment_id)
        result = await self._db.exec(stmt)
        return result.first()

    async def get_by_checkout_session_id(self, checkout_session_id: str) -> PaymentOrder | None:
        stmt = select(PaymentOrder).where(
            PaymentOrder.dodo_checkout_session_id == checkout_session_id
        )
        result = await self._db.exec(stmt)
        return result.first()

    async def list_by_user(
        self,
        *,
        user_id: uuid.UUID,
        channel: str | None,
        skip: int,
        limit: int,
    ) -> list[PaymentOrder]:
        stmt = (
            select(PaymentOrder)
            .where(PaymentOrder.user_id == user_id)
            .order_by(PaymentOrder.created_at.desc(), PaymentOrder.id.desc())
            .offset(skip)
            .limit(limit)
        )
        if channel is not None:
            stmt = stmt.where(PaymentOrder.channel == channel)
        result = await self._db.exec(stmt)
        return list(result.all())

    async def apply_update(
        self,
        order: PaymentOrder,
        *,
        update_data: Mapping[str, object],
    ) -> PaymentOrder:
        for field_name, value in update_data.items():
            setattr(order, field_name, value)
        await self._db.flush()
        return order

    async def mark_status(
        self,
        order: PaymentOrder,
        *,
        status: str,
        paid_at: datetime | None = None,
        refunded_at: datetime | None = None,
    ) -> PaymentOrder:
        order.status = status
        if paid_at is not None:
            order.paid_at = paid_at
        if refunded_at is not None:
            order.refunded_at = refunded_at
        await self._db.flush()
        return order
