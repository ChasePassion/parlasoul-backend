from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import UserAccessPass


class UserAccessPassRepository:
    def __init__(self, db: AsyncSession):
        self._db: AsyncSession = db

    async def get_by_source_order_id(self, source_order_id: uuid.UUID) -> UserAccessPass | None:
        stmt = select(UserAccessPass).where(UserAccessPass.source_order_id == source_order_id)
        result = await self._db.exec(stmt)
        return result.first()

    async def get_highest_active_for_user(self, user_id: uuid.UUID) -> UserAccessPass | None:
        now = datetime.now(timezone.utc)
        stmt = (
            select(UserAccessPass)
            .where(
                UserAccessPass.user_id == user_id,
                UserAccessPass.status == "active",
                UserAccessPass.ends_at > now,
            )
            .order_by(UserAccessPass.ends_at.desc(), UserAccessPass.created_at.desc())
        )
        result = await self._db.exec(stmt)
        passes = list(result.all())
        if not passes:
            return None

        def _rank(item: UserAccessPass) -> tuple[int, datetime]:
            tier_rank = 2 if item.tier == "pro" else 1
            return (tier_rank, item.ends_at)

        return max(passes, key=_rank)

    async def create(self, access_pass: UserAccessPass) -> UserAccessPass:
        self._db.add(access_pass)
        await self._db.flush()
        return access_pass

    async def upsert_active_pass(
        self,
        *,
        user_id: uuid.UUID,
        source_order_id: uuid.UUID,
        channel: str,
        product_id: str,
        tier: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> UserAccessPass:
        existing = await self.get_by_source_order_id(source_order_id)
        if existing is not None:
            existing.user_id = user_id
            existing.channel = channel
            existing.dodo_product_id = product_id
            existing.tier = tier
            existing.starts_at = starts_at
            existing.ends_at = ends_at
            existing.status = "active"
            await self._db.flush()
            return existing

        access_pass = UserAccessPass(
            user_id=user_id,
            source_order_id=source_order_id,
            channel=channel,
            dodo_product_id=product_id,
            tier=tier,
            starts_at=starts_at,
            ends_at=ends_at,
            status="active",
        )
        self._db.add(access_pass)
        await self._db.flush()
        return access_pass

    async def mark_status(self, access_pass: UserAccessPass, *, status: str) -> UserAccessPass:
        access_pass.status = status
        await self._db.flush()
        return access_pass
