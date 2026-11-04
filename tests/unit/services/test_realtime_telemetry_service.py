from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.core.exceptions import APIError
from src.db.models import RealtimeCallSession
from src.schemas.realtime_telemetry import (
    RealtimeTelemetryEventsBatchRequest,
    RealtimeTelemetrySessionCreateRequest,
    RealtimeTelemetrySessionUpdateRequest,
)
from src.services.realtime_telemetry_service import RealtimeTelemetryService


class FakeUnitOfWork:
    @asynccontextmanager
    async def transactional(self):
        yield

    async def flush(self) -> None:
        return None


class FakeTelemetryRepository:
    def __init__(self) -> None:
        self.sessions: dict[tuple[uuid.UUID, uuid.UUID], RealtimeCallSession] = {}
        self.event_keys: set[tuple[uuid.UUID, str, int]] = set()
        self.turn_updates: list[dict[str, object]] = []
        self.chat = None

    async def get_owned_chat(self, *, chat_id: uuid.UUID, user_id: uuid.UUID):
        if self.chat and self.chat.id == chat_id and self.chat.user_id == user_id:
            return self.chat
        return None

    async def add_session(self, session: RealtimeCallSession) -> None:
        now = datetime.now(UTC)
        session.started_at = now
        session.created_at = now
        session.updated_at = now
        self.sessions[(session.observation_id, session.user_id)] = session

    async def get_session(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        for_update: bool = False,
    ):
        del for_update
        return self.sessions.get((observation_id, user_id))

    async def insert_events(self, rows: list[dict[str, object]]) -> int:
        accepted = 0
        for row in rows:
            key = (row["observation_id"], row["source"], row["seq"])
            if key not in self.event_keys:
                self.event_keys.add(key)
                accepted += 1
        return accepted

    async def upsert_turn_metrics(self, values: dict[str, object]) -> None:
        self.turn_updates.append(values)


def _build_service() -> tuple[RealtimeTelemetryService, FakeTelemetryRepository]:
    repo = FakeTelemetryRepository()
    return RealtimeTelemetryService(repo, FakeUnitOfWork()), repo  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_session_validates_owned_chat_character_context() -> None:
    service, repo = _build_service()
    user_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    character_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id)
    repo.chat = SimpleNamespace(id=chat_id, user_id=user_id, character_id=character_id)

    response = await service.create_session(
        current_user=user,  # type: ignore[arg-type]
        request=RealtimeTelemetrySessionCreateRequest(
            mode="chat",
            chat_id=chat_id,
            character_id=character_id,
            scenario="normal",
        ),
    )

    assert (response.observation_id, user_id) in repo.sessions

    with pytest.raises(APIError) as exc:
        await service.create_session(
            current_user=user,  # type: ignore[arg-type]
            request=RealtimeTelemetrySessionCreateRequest(
                mode="chat",
                chat_id=chat_id,
                character_id=uuid.uuid4(),
            ),
        )
    assert exc.value.code == "resource_not_found"


@pytest.mark.asyncio
async def test_client_event_batch_is_idempotent_and_derives_turn_metrics() -> None:
    service, repo = _build_service()
    user_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id)
    repo.sessions[(observation_id, user_id)] = RealtimeCallSession(
        observation_id=observation_id,
        user_id=user_id,
        mode="lab",
    )
    request = RealtimeTelemetryEventsBatchRequest.model_validate(
        {
            "events": [
                {
                    "seq": 1,
                    "event_type": "audio.first_audible",
                    "occurred_offset_ms": 1000,
                    "payload": {
                        "generation_id": 2,
                        "ttfa_ms": 800,
                        "playout_latency_ms": 190,
                    },
                },
                {
                    "seq": 2,
                    "event_type": "barge_in.confirmed",
                    "occurred_offset_ms": 1500,
                    "payload": {"generation_id": 2, "elapsed_ms": 140},
                },
            ]
        }
    )

    first = await service.ingest_client_events(
        current_user=user,  # type: ignore[arg-type]
        observation_id=observation_id,
        request=request,
    )
    second = await service.ingest_client_events(
        current_user=user,  # type: ignore[arg-type]
        observation_id=observation_id,
        request=request,
    )

    assert first.accepted_count == 2
    assert first.duplicate_count == 0
    assert second.accepted_count == 0
    assert second.duplicate_count == 2
    assert repo.turn_updates[0]["ttfa_ms"] == 800
    assert repo.turn_updates[0]["client_first_audio_played_latency_ms"] == 190
    assert repo.turn_updates[1]["barge_in_stop_latency_ms"] == 140


@pytest.mark.asyncio
async def test_attach_runtime_session_requires_owned_matching_nonterminal_context() -> None:
    service, repo = _build_service()
    user_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    character_id = uuid.uuid4()
    session = RealtimeCallSession(
        observation_id=observation_id,
        user_id=user_id,
        chat_id=chat_id,
        character_id=character_id,
    )
    repo.sessions[(observation_id, user_id)] = session

    await service.attach_runtime_session(
        observation_id=observation_id,
        user_id=user_id,
        chat_id=chat_id,
        character_id=character_id,
        rtc_session_id="rt_deadbeef",
    )
    assert session.rtc_session_id == "rt_deadbeef"

    with pytest.raises(APIError) as exc:
        await service.attach_runtime_session(
            observation_id=observation_id,
            user_id=user_id,
            chat_id=chat_id,
            character_id=character_id,
            rtc_session_id="rt_deadbeef",
        )
    assert exc.value.code == "resource_conflict"


@pytest.mark.asyncio
async def test_public_update_uses_server_wall_clock_and_typed_summary() -> None:
    service, repo = _build_service()
    user_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    now = datetime.now(UTC)
    session = RealtimeCallSession(
        observation_id=observation_id,
        user_id=user_id,
        mode="lab",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    repo.sessions[(observation_id, user_id)] = session

    response = await service.update_session(
        current_user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
        observation_id=observation_id,
        request=RealtimeTelemetrySessionUpdateRequest.model_validate(
            {
                "status": "failed",
                "failure_stage": "ice",
                "error_code": "ice_failed",
                "duration_ms": 900,
                "summary": {"avg_rtt_ms": 55, "packet_loss_ratio": 0.02},
            }
        ),
    )

    assert response.ended_at is not None
    assert response.duration_ms == 900
    assert response.summary.avg_rtt_ms == 55
    assert response.summary.packet_loss_ratio == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_first_terminal_status_wins_and_late_client_patch_merges_aggregates() -> None:
    service, repo = _build_service()
    user_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    now = datetime.now(UTC)
    session = RealtimeCallSession(
        observation_id=observation_id,
        user_id=user_id,
        mode="lab",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    repo.sessions[(observation_id, user_id)] = session

    await service.update_session(
        current_user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
        observation_id=observation_id,
        request=RealtimeTelemetrySessionUpdateRequest(
            status="failed",
            failure_stage="ice",
            error_code="connection_failed",
            duration_ms=800,
            turn_count=1,
            summary={"failed_turn_count": 1},
        ),
    )
    response = await service.update_session(
        current_user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
        observation_id=observation_id,
        request=RealtimeTelemetrySessionUpdateRequest(
            status="completed",
            duration_ms=1_000,
            turn_count=2,
            summary={"avg_rtt_ms": 45},
        ),
    )

    assert response.status.value == "failed"
    assert response.failure_stage.value == "ice"
    assert response.error_code == "connection_failed"
    assert response.duration_ms == 1_000
    assert response.turn_count == 2
    assert response.summary.failed_turn_count == 1
    assert response.summary.avg_rtt_ms == 45
