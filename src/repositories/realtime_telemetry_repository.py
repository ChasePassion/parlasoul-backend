"""Data access for privacy-safe realtime call telemetry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import (
    Chat,
    RealtimeCallEvent,
    RealtimeCallSession,
    RealtimeTurnMetric,
    RealtimeWebRTCStatsSample,
)


class RealtimeTelemetryRepository:
    """All reads include user ownership even though PostgreSQL RLS also enforces it."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def add_session(self, session: RealtimeCallSession) -> None:
        self._db.add(session)
        await self._db.flush()

    async def get_session(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        for_update: bool = False,
    ) -> RealtimeCallSession | None:
        stmt = select(RealtimeCallSession).where(
            RealtimeCallSession.observation_id == observation_id,
            RealtimeCallSession.user_id == user_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def get_owned_chat(
        self,
        *,
        chat_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Chat | None:
        stmt = select(Chat).where(Chat.id == chat_id, Chat.user_id == user_id)
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def list_sessions(
        self,
        *,
        user_id: uuid.UUID,
        mode: str | None,
        status: str | None,
        scenario: str | None,
        cursor_started_at: datetime | None,
        cursor_observation_id: uuid.UUID | None,
        limit: int,
    ) -> tuple[list[RealtimeCallSession], bool]:
        stmt = select(RealtimeCallSession).where(RealtimeCallSession.user_id == user_id)
        if mode is not None:
            stmt = stmt.where(RealtimeCallSession.mode == mode)
        if status is not None:
            stmt = stmt.where(RealtimeCallSession.status == status)
        if scenario is not None:
            stmt = stmt.where(RealtimeCallSession.scenario == scenario)
        if cursor_started_at is not None and cursor_observation_id is not None:
            stmt = stmt.where(
                or_(
                    RealtimeCallSession.started_at < cursor_started_at,
                    and_(
                        RealtimeCallSession.started_at == cursor_started_at,
                        RealtimeCallSession.observation_id < cursor_observation_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            desc(RealtimeCallSession.started_at),
            desc(RealtimeCallSession.observation_id),
        ).limit(limit + 1)
        rows = list((await self._db.execute(stmt)).scalars().all())
        return rows[:limit], len(rows) > limit

    async def insert_events(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        stmt = pg_insert(RealtimeCallEvent).values(rows)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["observation_id", "source", "seq"]
        )
        result = await self._db.execute(stmt)
        return int(result.rowcount or 0)

    async def insert_stats_samples(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        stmt = pg_insert(RealtimeWebRTCStatsSample).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=["observation_id", "sample_seq"])
        result = await self._db.execute(stmt)
        return int(result.rowcount or 0)

    async def upsert_turn_metrics(self, values: dict[str, Any]) -> None:
        stmt = pg_insert(RealtimeTurnMetric).values(values)
        mutable = {
            key: value
            for key, value in values.items()
            if key
            not in {"observation_id", "user_id", "turn_seq", "generation_id", "created_at"}
        }
        mutable["updated_at"] = datetime.now(UTC)
        stmt = stmt.on_conflict_do_update(
            index_elements=["observation_id", "generation_id"],
            set_=mutable,
        )
        await self._db.execute(stmt)

    async def list_events(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int,
    ) -> list[RealtimeCallEvent]:
        stmt = (
            select(RealtimeCallEvent)
            .where(
                RealtimeCallEvent.observation_id == observation_id,
                RealtimeCallEvent.user_id == user_id,
            )
            .order_by(RealtimeCallEvent.occurred_offset_ms, RealtimeCallEvent.id)
            .limit(limit)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def list_turn_metrics(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int,
    ) -> list[RealtimeTurnMetric]:
        stmt = (
            select(RealtimeTurnMetric)
            .where(
                RealtimeTurnMetric.observation_id == observation_id,
                RealtimeTurnMetric.user_id == user_id,
            )
            .order_by(RealtimeTurnMetric.turn_seq, RealtimeTurnMetric.id)
            .limit(limit)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def list_stats_samples(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int,
    ) -> list[RealtimeWebRTCStatsSample]:
        stmt = (
            select(RealtimeWebRTCStatsSample)
            .where(
                RealtimeWebRTCStatsSample.observation_id == observation_id,
                RealtimeWebRTCStatsSample.user_id == user_id,
            )
            .order_by(
                RealtimeWebRTCStatsSample.sampled_offset_ms,
                RealtimeWebRTCStatsSample.id,
            )
            .limit(limit)
        )
        return list((await self._db.execute(stmt)).scalars().all())


__all__ = ["RealtimeTelemetryRepository"]
