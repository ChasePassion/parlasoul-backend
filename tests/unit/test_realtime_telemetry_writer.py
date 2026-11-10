from __future__ import annotations

import uuid

import pytest

from src.realtime.telemetry.writer import (
    RealtimeTelemetryRecorder,
)


@pytest.mark.asyncio
async def test_recorder_accepts_runtime_events_and_maps_offsets(monkeypatch) -> None:
    recorder = RealtimeTelemetryRecorder(
        observation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        runtime_session_id="rt_abc123",
    )
    captured: list[object] = []
    monkeypatch.setattr(recorder, "_enqueue", captured.append)

    await recorder.record_event(
        session_id="rt_abc123",
        event_type="state_transition",
        generation_id=2,
        stage="state_machine",
        reason_code="first_audio_enqueued",
        metrics={
            "offset_ms": 1200,
            "from_state": "thinking",
            "to_state": "agent_speaking",
        },
    )

    operation = captured[0]
    assert operation.event.occurred_offset_ms == 1200  # type: ignore[attr-defined]
    assert operation.event.payload == {  # type: ignore[attr-defined]
        "previous_state": "thinking",
        "state": "agent_speaking",
        "generation_id": 2,
    }


