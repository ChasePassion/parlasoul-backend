"""Idempotent evaluation persistence.

Contract highlights:
- every write uses its own session; concurrent case tasks never share one;
- the RLS session context is set to ``system`` before the transaction begins;
- results upsert on ``UNIQUE(run_id, case_id, runner)``; retries bump
  ``attempt`` and keep only the latest state;
- cancellation never overwrites already-completed rows;
- run counters are derived from result rows at finalize time (no racy
  increments).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import delete as sm_delete

from src.db.models import EvalResult, EvalRun
from src.db.session import AsyncSessionLocal

_RESULT_CONFLICT_KEY = ("run_id", "case_id", "runner")

# Statuses that must never be overwritten by a late cancel or retry.
_PROTECTED_RESULT_STATUSES = ("completed",)


class DbWriter:
    def __init__(self, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or AsyncSessionLocal

    @asynccontextmanager
    async def _system_session(self) -> AsyncIterator:
        async with self._session_factory() as session:
            session.info["current_user_id"] = "system"
            yield session

    # -- runs ---------------------------------------------------------

    async def create_run(self, run: EvalRun) -> uuid.UUID:
        async with self._system_session() as session:
            session.add(run)
            await session.commit()
        return run.id

    async def get_run(self, run_id: uuid.UUID) -> EvalRun | None:
        async with self._system_session() as session:
            return await session.get(EvalRun, run_id)

    async def mark_run_status(
        self,
        run_id: uuid.UUID,
        status: str,
        *,
        finished_at: datetime | None = None,
    ) -> None:
        async with self._system_session() as session:
            await session.execute(
                update(EvalRun)
                .where(EvalRun.id == run_id)
                .values(
                    status=status,
                    finished_at=finished_at or datetime.now(UTC),
                )
            )
            await session.commit()

    async def finalize_run(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        run_kind: str,
        summary_json: dict[str, Any],
    ) -> None:
        """Set final status/summary and derive counters from result rows."""
        async with self._system_session() as session:
            counts = {"completed": 0, "failed": 0, "skipped": 0, "no_data": 0, "cancelled": 0}
            if run_kind == "case":
                rows = await session.execute(
                    select(EvalResult.status, func.count())
                    .where(EvalResult.run_id == run_id)
                    .group_by(EvalResult.status)
                )
                for status_value, count in rows.all():
                    if status_value in counts:
                        counts[status_value] = count

            await session.execute(
                update(EvalRun)
                .where(EvalRun.id == run_id)
                .values(
                    status=status,
                    summary_json=summary_json,
                    completed_case_count=counts["completed"],
                    # any non-valid terminal response counts as failed for the
                    # run denominator (skipped/no_data stay out of "failed")
                    failed_case_count=counts["failed"],
                    cancelled_case_count=counts["cancelled"],
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()

    # -- results ---------------------------------------------------------

    async def upsert_result(self, result: EvalResult) -> None:
        values = {
            column.name: getattr(result, column.name)
            for column in EvalResult.__table__.columns
            if column.name != "id"
        }
        # Let server_default (now()) apply instead of inserting explicit NULL.
        if values.get("created_at") is None:
            values.pop("created_at")
        stmt = pg_insert(EvalResult).values(id=result.id, **values)
        conflict_update = {name: stmt.excluded[name] for name in values}
        conflict_update["attempt"] = EvalResult.attempt + 1
        stmt = stmt.on_conflict_do_update(
            index_elements=list(_RESULT_CONFLICT_KEY),
            set_=conflict_update,
        )
        async with self._system_session() as session:
            await session.execute(stmt)
            await session.commit()

    async def mark_cases_cancelled(
        self,
        *,
        run_id: uuid.UUID,
        runner: str,
        case_ids: list[str],
        content_mode: str,
        owner_user_id: uuid.UUID | None = None,
    ) -> int:
        """Idempotently record cancelled state for cases that never completed.

        Completed rows are protected; rows already persisted with any other
        state become cancelled, queued rows get a fresh cancelled row.
        Returns the number of rows written.
        """
        if not case_ids:
            return 0
        now = datetime.now(UTC)
        async with self._system_session() as session:
            for case_id in case_ids:
                values = {
                    "id": uuid.uuid4(),
                    "run_id": run_id,
                    "case_id": case_id,
                    "runner": runner,
                    "status": "cancelled",
                    "response_status": "cancelled",
                    "attempt": 1,
                    "content_mode": content_mode,
                    "input_ref": f"case:{case_id}",
                    "output_ref": f"case:{case_id}",
                    "owner_user_id": owner_user_id,
                    "started_at": now,
                    "finished_at": now,
                    "duration_ms": 0,
                }
                stmt = pg_insert(EvalResult).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(_RESULT_CONFLICT_KEY),
                    set_={
                        "status": stmt.excluded.status,
                        "response_status": stmt.excluded.response_status,
                        "finished_at": stmt.excluded.finished_at,
                        "attempt": EvalResult.attempt + 1,
                    },
                    where=EvalResult.status.notin_(_PROTECTED_RESULT_STATUSES),
                )
                await session.execute(stmt)
            await session.commit()
        return len(case_ids)

    async def count_results_by_response_status(
        self, run_id: uuid.UUID
    ) -> dict[str, int]:
        async with self._system_session() as session:
            rows = await session.execute(
                select(EvalResult.response_status, func.count())
                .where(EvalResult.run_id == run_id)
                .group_by(EvalResult.response_status)
            )
            return {status: int(count) for status, count in rows.all()}

    # -- retention ---------------------------------------------------------

    async def delete_run(self, run_id: uuid.UUID) -> None:
        """Delete a run and cascade its results (prune path)."""
        async with self._system_session() as session:
            await session.execute(sm_delete(EvalRun).where(EvalRun.id == run_id))
            await session.commit()
