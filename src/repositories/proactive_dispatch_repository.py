"""Persistent queue repository for proactive message dispatches."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import ProactiveDispatchStatus, ProactiveMessageDispatch


class ProactiveDispatchRepository:
    """Claim/update queue rows for proactive message generation."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def enqueue_unique(
        self,
        *,
        user_id: uuid.UUID,
        slot_at: datetime,
        slot_label: str,
        timezone: str,
        payload_json: dict[str, object] | None = None,
    ) -> bool:
        stmt = pg_insert(ProactiveMessageDispatch).values(
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "slot_at": slot_at,
                "slot_label": slot_label,
                "timezone": timezone,
                "status": ProactiveDispatchStatus.QUEUED.value,
                "payload_json": payload_json or {},
            }
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "slot_at"])
        result = await self._db.execute(stmt)
        return bool(result.rowcount or 0)

    async def claim_due(self, *, now: datetime, lease_seconds: int) -> ProactiveMessageDispatch | None:
        stmt = (
            select(ProactiveMessageDispatch)
            .where(
                ProactiveMessageDispatch.next_attempt_at <= now,
                or_(
                    ProactiveMessageDispatch.status == ProactiveDispatchStatus.QUEUED.value,
                    ProactiveMessageDispatch.status == ProactiveDispatchStatus.RETRY_WAITING.value,
                    and_(
                        ProactiveMessageDispatch.status == ProactiveDispatchStatus.PROCESSING.value,
                        ProactiveMessageDispatch.lease_expires_at.is_not(None),
                        ProactiveMessageDispatch.lease_expires_at < now,
                    ),
                ),
            )
            .order_by(ProactiveMessageDispatch.next_attempt_at, ProactiveMessageDispatch.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await self._db.execute(stmt)
        dispatch = result.scalars().first()
        if dispatch is None:
            return None

        dispatch.status = ProactiveDispatchStatus.PROCESSING.value
        dispatch.attempt_count = int(dispatch.attempt_count) + 1
        dispatch.lease_expires_at = now + timedelta(seconds=lease_seconds)
        dispatch.error_code = None
        dispatch.error_message = None
        return dispatch

    async def mark_skipped(
        self,
        dispatch: ProactiveMessageDispatch,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        dispatch.status = ProactiveDispatchStatus.SKIPPED.value
        dispatch.error_code = error_code
        dispatch.error_message = error_message
        dispatch.lease_expires_at = None
        dispatch.completed_at = now or datetime.now(UTC)

    async def mark_succeeded(
        self,
        dispatch: ProactiveMessageDispatch,
        *,
        selected_character_id: uuid.UUID | None,
        payload_json: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> None:
        dispatch.status = ProactiveDispatchStatus.SUCCEEDED.value
        dispatch.selected_character_id = selected_character_id
        if payload_json is not None:
            dispatch.payload_json = payload_json
        dispatch.lease_expires_at = None
        dispatch.completed_at = now or datetime.now(UTC)

    async def mark_failed(
        self,
        dispatch: ProactiveMessageDispatch,
        *,
        error_code: str | None,
        error_message: str | None,
        now: datetime | None = None,
    ) -> None:
        dispatch.status = ProactiveDispatchStatus.FAILED.value
        dispatch.error_code = error_code
        dispatch.error_message = error_message
        dispatch.lease_expires_at = None
        dispatch.completed_at = now or datetime.now(UTC)

    async def schedule_retry(
        self,
        dispatch: ProactiveMessageDispatch,
        *,
        error_code: str | None,
        error_message: str | None,
        next_attempt_at: datetime,
        selected_character_id: uuid.UUID | None = None,
    ) -> None:
        dispatch.status = ProactiveDispatchStatus.RETRY_WAITING.value
        dispatch.error_code = error_code
        dispatch.error_message = error_message
        dispatch.selected_character_id = selected_character_id
        dispatch.next_attempt_at = next_attempt_at
        dispatch.lease_expires_at = None
