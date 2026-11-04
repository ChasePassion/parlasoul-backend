"""Repository helpers for Phase 5 growth aggregates."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import asc, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import (
    Character,
    GrowthCharacterDailyStats,
    GrowthCharacterStats,
    GrowthDailyStats,
    GrowthShareTrigger,
    GrowthUserStats,
)
class GrowthRepository:
    def __init__(self, db: AsyncSession):
        self._db = db

    @staticmethod
    def _maybe_for_update(stmt, *, for_update: bool):
        return stmt.with_for_update() if for_update else stmt

    async def get_user_stats(self, user_id: uuid.UUID, *, for_update: bool = False) -> GrowthUserStats | None:
        stmt = self._maybe_for_update(
            select(GrowthUserStats).where(GrowthUserStats.user_id == user_id),
            for_update=for_update,
        )
        return (await self._db.execute(stmt)).scalars().first()

    async def get_or_create_user_stats(self, user_id: uuid.UUID, *, for_update: bool = False) -> GrowthUserStats:
        stats = await self.get_user_stats(user_id, for_update=for_update)
        if stats:
            return stats
        try:
            async with self._db.begin_nested():
                stats = GrowthUserStats(user_id=user_id)
                self._db.add(stats)
                await self._db.flush()
        except IntegrityError:
            pass
        stats = await self.get_user_stats(user_id, for_update=for_update)
        if stats is None:
            raise RuntimeError(f"growth_user_stats row missing after get_or_create; user_id={user_id}")
        return stats

    async def get_daily_stats(self, user_id: uuid.UUID, stat_date: date, *, for_update: bool = False) -> GrowthDailyStats | None:
        stmt = self._maybe_for_update(select(GrowthDailyStats).where(
            GrowthDailyStats.user_id == user_id,
            GrowthDailyStats.stat_date == stat_date,
        ), for_update=for_update)
        return (await self._db.execute(stmt)).scalars().first()

    async def get_or_create_daily_stats(
        self,
        user_id: uuid.UUID,
        stat_date: date,
        *,
        for_update: bool = False,
    ) -> GrowthDailyStats:
        stats = await self.get_daily_stats(user_id, stat_date, for_update=for_update)
        if stats:
            return stats
        try:
            async with self._db.begin_nested():
                stats = GrowthDailyStats(id=uuid.uuid4(), user_id=user_id, stat_date=stat_date)
                self._db.add(stats)
                await self._db.flush()
        except IntegrityError:
            pass
        stats = await self.get_daily_stats(user_id, stat_date, for_update=for_update)
        if stats is None:
            raise RuntimeError(
                f"growth_daily_stats row missing after get_or_create; user_id={user_id}; stat_date={stat_date}"
            )
        return stats

    async def list_daily_stats_range(
        self,
        user_id: uuid.UUID,
        *,
        start_date: date,
        end_date: date,
    ) -> list[GrowthDailyStats]:
        stmt = (
            select(GrowthDailyStats)
            .where(
                GrowthDailyStats.user_id == user_id,
                GrowthDailyStats.stat_date >= start_date,
                GrowthDailyStats.stat_date <= end_date,
            )
            .order_by(asc(GrowthDailyStats.stat_date))
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def list_character_daily_stats_range(
        self,
        user_id: uuid.UUID,
        *,
        start_date: date,
        end_date: date,
    ) -> list[tuple[GrowthCharacterDailyStats, Character]]:
        stmt = (
            select(GrowthCharacterDailyStats, Character)
            .join(Character, Character.id == GrowthCharacterDailyStats.character_id)
            .where(
                GrowthCharacterDailyStats.user_id == user_id,
                GrowthCharacterDailyStats.stat_date >= start_date,
                GrowthCharacterDailyStats.stat_date <= end_date,
                GrowthCharacterDailyStats.total_message_count > 0,
            )
            .order_by(
                asc(GrowthCharacterDailyStats.stat_date),
                desc(GrowthCharacterDailyStats.total_message_count),
                desc(GrowthCharacterDailyStats.total_word_count),
                desc(GrowthCharacterDailyStats.character_id),
            )
        )
        return list((await self._db.execute(stmt)).all())

    async def get_character_daily_stats(
        self,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        stat_date: date,
        *,
        for_update: bool = False,
    ) -> GrowthCharacterDailyStats | None:
        stmt = self._maybe_for_update(select(GrowthCharacterDailyStats).where(
            GrowthCharacterDailyStats.user_id == user_id,
            GrowthCharacterDailyStats.character_id == character_id,
            GrowthCharacterDailyStats.stat_date == stat_date,
        ), for_update=for_update)
        return (await self._db.execute(stmt)).scalars().first()

    async def get_or_create_character_daily_stats(
        self,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        stat_date: date,
        *,
        for_update: bool = False,
    ) -> GrowthCharacterDailyStats:
        stats = await self.get_character_daily_stats(user_id, character_id, stat_date, for_update=for_update)
        if stats:
            return stats
        try:
            async with self._db.begin_nested():
                stats = GrowthCharacterDailyStats(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    character_id=character_id,
                    stat_date=stat_date,
                )
                self._db.add(stats)
                await self._db.flush()
        except IntegrityError:
            pass
        stats = await self.get_character_daily_stats(user_id, character_id, stat_date, for_update=for_update)
        if stats is None:
            raise RuntimeError(
                "growth_character_daily_stats row missing after get_or_create; "
                f"user_id={user_id}; character_id={character_id}; stat_date={stat_date}"
            )
        return stats

    async def get_character_stats(
        self,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> GrowthCharacterStats | None:
        stmt = self._maybe_for_update(select(GrowthCharacterStats).where(
            GrowthCharacterStats.user_id == user_id,
            GrowthCharacterStats.character_id == character_id,
        ), for_update=for_update)
        return (await self._db.execute(stmt)).scalars().first()

    async def get_or_create_character_stats(
        self,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> GrowthCharacterStats:
        stats = await self.get_character_stats(user_id, character_id, for_update=for_update)
        if stats:
            return stats
        try:
            async with self._db.begin_nested():
                stats = GrowthCharacterStats(id=uuid.uuid4(), user_id=user_id, character_id=character_id)
                self._db.add(stats)
                await self._db.flush()
        except IntegrityError:
            pass
        stats = await self.get_character_stats(user_id, character_id, for_update=for_update)
        if stats is None:
            raise RuntimeError(
                f"growth_character_stats row missing after get_or_create; user_id={user_id}; character_id={character_id}"
            )
        return stats

    async def list_character_stats(
        self,
        user_id: uuid.UUID,
        *,
        only_with_exchanges: bool = False,
    ) -> list[tuple[GrowthCharacterStats, Character]]:
        stmt = (
            select(GrowthCharacterStats, Character)
            .join(Character, Character.id == GrowthCharacterStats.character_id)
            .where(GrowthCharacterStats.user_id == user_id)
        )
        if only_with_exchanges:
            stmt = stmt.where(GrowthCharacterStats.total_exchange_count > 0)
        return list((await self._db.execute(stmt)).all())

    async def get_share_trigger_by_key(self, trigger_key: str) -> GrowthShareTrigger | None:
        stmt = select(GrowthShareTrigger).where(GrowthShareTrigger.trigger_key == trigger_key)
        return (await self._db.execute(stmt)).scalars().first()

    async def create_share_trigger_if_absent(
        self,
        *,
        user_id: uuid.UUID,
        chat_id: uuid.UUID | None,
        character_id: uuid.UUID | None,
        trigger_kind: str,
        trigger_key: str,
        payload: dict,
    ) -> tuple[GrowthShareTrigger, bool]:
        existing = await self.get_share_trigger_by_key(trigger_key)
        if existing:
            return existing, False

        try:
            async with self._db.begin_nested():
                trigger = GrowthShareTrigger(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    chat_id=chat_id,
                    character_id=character_id,
                    trigger_kind=trigger_kind,
                    trigger_key=trigger_key,
                    payload=payload,
                )
                self._db.add(trigger)
                await self._db.flush()
                return trigger, True
        except IntegrityError:
            existing = await self.get_share_trigger_by_key(trigger_key)
            if existing is None:
                raise
            return existing, False

    async def list_pending_share_triggers(
        self,
        *,
        user_id: uuid.UUID,
        chat_id: uuid.UUID | None,
        limit: int,
    ) -> list[GrowthShareTrigger]:
        stmt = select(GrowthShareTrigger).where(
            GrowthShareTrigger.user_id == user_id,
            GrowthShareTrigger.consumed_at.is_(None),
        )
        if chat_id is not None:
            stmt = stmt.where(GrowthShareTrigger.chat_id == chat_id)
        stmt = stmt.order_by(asc(GrowthShareTrigger.triggered_at), asc(GrowthShareTrigger.id)).limit(limit)
        return list((await self._db.execute(stmt)).scalars().all())

    async def get_share_trigger_by_id(
        self,
        *,
        user_id: uuid.UUID,
        trigger_id: uuid.UUID,
    ) -> GrowthShareTrigger | None:
        stmt = select(GrowthShareTrigger).where(
            GrowthShareTrigger.user_id == user_id,
            GrowthShareTrigger.id == trigger_id,
        )
        return (await self._db.execute(stmt)).scalars().first()

    async def count_character_rows_with_exchanges(self, user_id: uuid.UUID) -> int:
        stmt = select(func.count(GrowthCharacterStats.id)).where(
            GrowthCharacterStats.user_id == user_id,
            GrowthCharacterStats.total_exchange_count > 0,
        )
        value = (await self._db.execute(stmt)).scalar_one()
        return int(value or 0)
