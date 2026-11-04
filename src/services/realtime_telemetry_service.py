"""Application service for persistent, privacy-safe realtime telemetry."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import APIError
from src.core.uow import UnitOfWork
from src.db.models import (
    RealtimeCallEvent,
    RealtimeCallSession,
    RealtimeTurnMetric,
    RealtimeWebRTCStatsSample,
    User,
)
from src.repositories.realtime_telemetry_repository import RealtimeTelemetryRepository
from src.schemas.realtime_telemetry import (
    RealtimeTelemetryBatchResponse,
    RealtimeTelemetryEventInput,
    RealtimeTelemetryEventResponse,
    RealtimeTelemetryEventsBatchRequest,
    RealtimeTelemetryMode,
    RealtimeTelemetryScenario,
    RealtimeTelemetrySessionCreateRequest,
    RealtimeTelemetrySessionCreateResponse,
    RealtimeTelemetrySessionDetailResponse,
    RealtimeTelemetrySessionPageResponse,
    RealtimeTelemetrySessionResponse,
    RealtimeTelemetrySessionUpdateRequest,
    RealtimeTelemetryStatus,
    RealtimeTurnMetricsResponse,
    RealtimeWebRTCStatsBatchRequest,
    RealtimeWebRTCStatsResponse,
    sanitize_event_payload,
    validate_rtc_session_id,
)

_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


def _model_from_attributes(model_type: type[_ResponseModel], row: object) -> _ResponseModel:
    return model_type.model_validate(
        {name: getattr(row, name) for name in model_type.model_fields}
    )


class RealtimeTelemetryService:
    """Owns authorization, lifecycle rules, privacy validation, and transactions."""

    def __init__(self, repo: RealtimeTelemetryRepository, uow: UnitOfWork):
        self._repo = repo
        self._uow = uow

    async def create_session(
        self,
        *,
        current_user: User,
        request: RealtimeTelemetrySessionCreateRequest,
    ) -> RealtimeTelemetrySessionCreateResponse:
        if request.chat_id is not None:
            chat = await self._repo.get_owned_chat(chat_id=request.chat_id, user_id=current_user.id)
            if chat is None or chat.character_id != request.character_id:
                raise APIError(
                    404,
                    "resource_not_found",
                    "resource not found: chat context missing",
                )

        session = RealtimeCallSession(
            user_id=current_user.id,
            chat_id=request.chat_id,
            character_id=request.character_id,
            mode=request.mode.value,
            scenario=request.scenario.value,
            experiment_tag=request.experiment_tag,
            client_build=request.client_build,
            browser_name=request.browser_name.value,
            browser_version=request.browser_version,
            os_name=request.os_name.value,
            os_version=request.os_version,
            safe_config=request.safe_config.model_dump(exclude_none=True, mode="json"),
            summary={},
        )
        async with self._uow.transactional():
            await self._repo.add_session(session)
        return RealtimeTelemetrySessionCreateResponse(observation_id=session.observation_id)

    async def ingest_client_events(
        self,
        *,
        current_user: User,
        observation_id: uuid.UUID,
        request: RealtimeTelemetryEventsBatchRequest,
    ) -> RealtimeTelemetryBatchResponse:
        await self._require_session(observation_id=observation_id, user_id=current_user.id)
        rows: list[dict[str, Any]] = []
        try:
            for event in request.events:
                rows.append(
                    self.build_event_row(
                        observation_id=observation_id,
                        user_id=current_user.id,
                        source="client",
                        event=event,
                    )
                )
        except (TypeError, ValueError) as exc:
            raise APIError(422, "validation_failed", f"validation failed: {exc}") from exc
        async with self._uow.transactional():
            accepted = await self._repo.insert_events(rows)
            for values in self._derive_client_turn_metrics(
                observation_id=observation_id,
                user_id=current_user.id,
                events=request.events,
            ):
                await self._repo.upsert_turn_metrics(values)
        return RealtimeTelemetryBatchResponse(
            accepted_count=accepted,
            duplicate_count=len(rows) - accepted,
        )

    async def ingest_stats_samples(
        self,
        *,
        current_user: User,
        observation_id: uuid.UUID,
        request: RealtimeWebRTCStatsBatchRequest,
    ) -> RealtimeTelemetryBatchResponse:
        await self._require_session(observation_id=observation_id, user_id=current_user.id)
        rows = [
            {
                "observation_id": observation_id,
                "user_id": current_user.id,
                **sample.model_dump(exclude_none=True, mode="json"),
            }
            for sample in request.samples
        ]
        async with self._uow.transactional():
            accepted = await self._repo.insert_stats_samples(rows)
        return RealtimeTelemetryBatchResponse(
            accepted_count=accepted,
            duplicate_count=len(rows) - accepted,
        )

    async def update_session(
        self,
        *,
        current_user: User,
        observation_id: uuid.UUID,
        request: RealtimeTelemetrySessionUpdateRequest,
    ) -> RealtimeTelemetrySessionResponse:
        now = datetime.now(UTC)
        async with self._uow.transactional():
            session = await self._require_session(
                observation_id=observation_id,
                user_id=current_user.id,
                for_update=True,
            )
            terminal_statuses = {
                RealtimeTelemetryStatus.COMPLETED.value,
                RealtimeTelemetryStatus.FAILED.value,
                RealtimeTelemetryStatus.CANCELLED.value,
            }
            terminal_conflict = bool(
                request.status is not None
                and session.status in terminal_statuses
                and session.status != request.status.value
            )
            if request.status is not None:
                if not terminal_conflict:
                    self._validate_status_transition(session.status, request.status)
                    session.status = request.status.value
                    if request.status == RealtimeTelemetryStatus.CONNECTED:
                        session.connected_at = session.connected_at or now
                    elif request.status.value in terminal_statuses:
                        session.ended_at = session.ended_at or now
            for name in (
                "failure_stage",
                "error_code",
                "duration_ms",
                "turn_count",
                "interruption_count",
            ):
                if name not in request.model_fields_set:
                    continue
                value = getattr(request, name)
                if name in {"failure_stage", "error_code"}:
                    if terminal_conflict or getattr(session, name) is not None:
                        continue
                    setattr(session, name, value.value if hasattr(value, "value") else value)
                    continue
                if value is not None:
                    setattr(session, name, max(getattr(session, name) or 0, value))
            if request.summary is not None:
                session.summary = {
                    **session.summary,
                    **request.summary.model_dump(exclude_unset=True, exclude_none=True),
                }
            session.updated_at = now
            await self._uow.flush()
        return self.to_session_response(session)

    async def attach_runtime_session(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        chat_id: uuid.UUID,
        character_id: uuid.UUID,
        rtc_session_id: str,
    ) -> None:
        """Associate an internal rt_* id; this is intentionally not exposed by PATCH."""

        normalized = validate_rtc_session_id(rtc_session_id)
        try:
            async with self._uow.transactional():
                session = await self._require_session(
                    observation_id=observation_id,
                    user_id=user_id,
                    for_update=True,
                )
                if session.chat_id != chat_id or session.character_id != character_id:
                    raise APIError(
                        409,
                        "resource_conflict",
                        "resource conflict: telemetry and realtime chat context differ",
                    )
                if session.status not in {
                    RealtimeTelemetryStatus.STARTING.value,
                    RealtimeTelemetryStatus.CONNECTED.value,
                }:
                    raise APIError(
                        409,
                        "resource_conflict",
                        "resource conflict: telemetry session is already terminal",
                    )
                if session.rtc_session_id is not None:
                    raise APIError(
                        409,
                        "resource_conflict",
                        "resource conflict: telemetry session is already attached",
                    )
                session.rtc_session_id = normalized
                session.updated_at = datetime.now(UTC)
                await self._uow.flush()
        except IntegrityError as exc:
            raise APIError(
                409,
                "resource_conflict",
                "resource conflict: runtime session is already attached",
            ) from exc

    async def list_sessions(
        self,
        *,
        current_user: User,
        mode: RealtimeTelemetryMode | None,
        status: RealtimeTelemetryStatus | None,
        scenario: RealtimeTelemetryScenario | None,
        cursor: str | None,
        limit: int,
    ) -> RealtimeTelemetrySessionPageResponse:
        cursor_at, cursor_id = self._decode_cursor(cursor)
        rows, has_more = await self._repo.list_sessions(
            user_id=current_user.id,
            mode=mode.value if mode else None,
            status=status.value if status else None,
            scenario=scenario.value if scenario else None,
            cursor_started_at=cursor_at,
            cursor_observation_id=cursor_id,
            limit=limit,
        )
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = self._encode_cursor(last.started_at, last.observation_id)
        return RealtimeTelemetrySessionPageResponse(
            items=[self.to_session_response(row) for row in rows],
            next_cursor=next_cursor,
        )

    async def get_session_detail(
        self,
        *,
        current_user: User,
        observation_id: uuid.UUID,
        events_limit: int,
        turns_limit: int,
        stats_limit: int,
    ) -> RealtimeTelemetrySessionDetailResponse:
        session = await self._require_session(
            observation_id=observation_id,
            user_id=current_user.id,
        )
        events = await self._repo.list_events(
            observation_id=observation_id,
            user_id=current_user.id,
            limit=events_limit,
        )
        turns = await self._repo.list_turn_metrics(
            observation_id=observation_id,
            user_id=current_user.id,
            limit=turns_limit,
        )
        samples = await self._repo.list_stats_samples(
            observation_id=observation_id,
            user_id=current_user.id,
            limit=stats_limit,
        )
        return RealtimeTelemetrySessionDetailResponse(
            session=self.to_session_response(session),
            events=[self.to_event_response(row) for row in events],
            turns=[self.to_turn_response(row) for row in turns],
            stats_samples=[self.to_stats_response(row) for row in samples],
        )

    async def _require_session(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        for_update: bool = False,
    ) -> RealtimeCallSession:
        session = await self._repo.get_session(
            observation_id=observation_id,
            user_id=user_id,
            for_update=for_update,
        )
        if session is None:
            raise APIError(404, "resource_not_found", "resource not found: telemetry session missing")
        return session

    @staticmethod
    def build_event_row(
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        source: str,
        event: RealtimeTelemetryEventInput,
    ) -> dict[str, Any]:
        return {
            "observation_id": observation_id,
            "user_id": user_id,
            "source": source,
            "seq": event.seq,
            "event_type": event.event_type,
            "stage": event.stage.value if event.stage else None,
            "reason_code": event.reason_code,
            "occurred_offset_ms": event.occurred_offset_ms,
            "payload": sanitize_event_payload(event.payload),
        }

    @staticmethod
    def _derive_client_turn_metrics(
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        events: list[RealtimeTelemetryEventInput],
    ) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for event in events:
            generation_id = event.payload.get("generation_id")
            if (
                isinstance(generation_id, bool)
                or not isinstance(generation_id, int)
                or generation_id < 0
            ):
                continue
            values: dict[str, Any] = {
                "observation_id": observation_id,
                "user_id": user_id,
                "turn_seq": generation_id,
                "generation_id": generation_id,
            }
            if event.event_type == "audio.first_audible":
                ttfa_ms = event.payload.get("ttfa_ms")
                playout_ms = event.payload.get("playout_latency_ms")
                if isinstance(ttfa_ms, (int, float)) and not isinstance(ttfa_ms, bool):
                    values["ttfa_ms"] = int(ttfa_ms)
                if isinstance(playout_ms, (int, float)) and not isinstance(playout_ms, bool):
                    values["client_first_audio_played_latency_ms"] = int(playout_ms)
            elif event.event_type in {"barge_in.confirmed", "barge_in.audio_stopped"}:
                stop_ms = event.payload.get("barge_in_stop_latency_ms")
                if stop_ms is None:
                    stop_ms = event.payload.get("elapsed_ms")
                if isinstance(stop_ms, (int, float)) and not isinstance(stop_ms, bool):
                    values["barge_in_stop_latency_ms"] = int(stop_ms)
            if len(values) > 4:
                updates.append(values)
        return updates

    @staticmethod
    def to_session_response(row: RealtimeCallSession) -> RealtimeTelemetrySessionResponse:
        values = {name: getattr(row, name) for name in RealtimeTelemetrySessionResponse.model_fields}
        values["safe_config"] = row.safe_config or {}
        values["summary"] = row.summary or {}
        return RealtimeTelemetrySessionResponse.model_validate(values)

    @staticmethod
    def to_event_response(row: RealtimeCallEvent) -> RealtimeTelemetryEventResponse:
        return _model_from_attributes(RealtimeTelemetryEventResponse, row)

    @staticmethod
    def to_turn_response(row: RealtimeTurnMetric) -> RealtimeTurnMetricsResponse:
        return _model_from_attributes(RealtimeTurnMetricsResponse, row)

    @staticmethod
    def to_stats_response(row: RealtimeWebRTCStatsSample) -> RealtimeWebRTCStatsResponse:
        return _model_from_attributes(RealtimeWebRTCStatsResponse, row)

    @staticmethod
    def _validate_status_transition(current: str, target: RealtimeTelemetryStatus) -> None:
        allowed = {
            "starting": {"starting", "connected", "failed", "cancelled"},
            "connected": {"connected", "completed", "failed", "cancelled"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        if target.value not in allowed.get(current, set()):
            raise APIError(
                409,
                "resource_conflict",
                f"resource conflict: cannot transition telemetry session from {current} to {target.value}",
            )

    @staticmethod
    def _encode_cursor(started_at: datetime | None, observation_id: uuid.UUID) -> str:
        if started_at is None:
            raise RuntimeError("persisted telemetry session is missing started_at")
        payload = json.dumps([started_at.isoformat(), str(observation_id)], separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[datetime | None, uuid.UUID | None]:
        if cursor is None:
            return None, None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            started_at_raw, observation_id_raw = json.loads(raw)
            started_at = datetime.fromisoformat(started_at_raw)
            if started_at.tzinfo is None:
                raise ValueError("cursor timestamp must be timezone-aware")
            return started_at, uuid.UUID(observation_id_raw)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
            raise APIError(400, "invalid_param", "invalid request: cursor is invalid") from exc


__all__ = ["RealtimeTelemetryService"]
