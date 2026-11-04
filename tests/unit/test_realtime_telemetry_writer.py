from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from src.realtime.telemetry.writer import (
    RealtimeTelemetryRecorder,
    RealtimeTelemetryRecorderFactory,
)
from src.schemas.realtime_telemetry import RealtimeTelemetrySessionUpdateRequest


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


@pytest.mark.asyncio
async def test_recorder_aggregates_real_runtime_turn_metrics(monkeypatch) -> None:
    recorder = RealtimeTelemetryRecorder(
        observation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        runtime_session_id="rt_abc123",
    )
    events: list[object] = []
    critical: list[object] = []
    monkeypatch.setattr(recorder, "_enqueue", events.append)

    async def _capture(operation: object) -> None:
        critical.append(operation)

    monkeypatch.setattr(recorder, "_enqueue_critical", _capture)

    await recorder.record_event(
        session_id="rt_abc123",
        event_type="speech_stop",
        generation_id=1,
        stage="vad",
        metrics={"offset_ms": 900, "confidence": 0.8, "voiced_ms": 600},
    )
    await recorder.record_event(
        session_id="rt_abc123",
        event_type="end_of_turn_confirmed",
        generation_id=1,
        stage="endpointing",
        metrics={"offset_ms": 1000, "utterance_ms": 800, "voiced_ms": 600},
    )
    await recorder.record_event(
        session_id="rt_abc123",
        event_type="stt_final",
        generation_id=1,
        stage="stt",
        metrics={"latency_ms": 220, "recognized_char_count": 15, "is_empty": False},
    )
    await recorder.record_event(
        session_id="rt_abc123",
        event_type="llm_request",
        generation_id=1,
        stage="llm",
        metrics={"offset_ms": 1300},
    )
    await recorder.record_event(
        session_id="rt_abc123",
        event_type="llm_first_token",
        generation_id=1,
        stage="llm",
        metrics={"offset_ms": 1480, "latency_ms": 180},
    )
    await recorder.record_event(
        session_id="rt_abc123",
        event_type="first_speakable_chunk",
        generation_id=1,
        stage="llm",
        metrics={"offset_ms": 1550, "speakable_char_count": 20},
    )
    await recorder.record_event(
        session_id="rt_abc123",
        event_type="tts_request_start",
        generation_id=1,
        stage="tts",
        metrics={"offset_ms": 1560},
    )
    await recorder.record_event(
        session_id="rt_abc123",
        event_type="tts_first_audio",
        generation_id=1,
        stage="tts",
        metrics={"offset_ms": 1640, "pacer_relative_ms": 90},
    )
    await recorder.upsert_turn_metrics(
        session_id="rt_abc123",
        generation_id=1,
        metrics={
            "outcome": "interrupted",
            "buffered_ms": 0,
            "peak_buffered_ms": 360,
            "underrun_count": 2,
            "partial_pad_count": 1,
            "frames_read": 40,
            "silence_frames": 3,
            "rejected_write_count": 1,
            "fadeout_count": 1,
            "first_audio_enqueued_ms": 90,
            "server_first_audio_read_ms": 110,
            "server_rtp_source_drained_ms": 900,
        },
    )

    metrics = critical[0].metrics  # type: ignore[attr-defined]
    assert metrics.outcome.value == "interrupted"
    assert metrics.speech_duration_ms == 800
    assert metrics.endpointing_latency_ms == 100
    assert metrics.stt_final_latency_ms == 220
    assert metrics.llm_ttft_ms == 180
    assert metrics.first_speakable_chunk_latency_ms == 550
    assert metrics.tts_ttfb_ms == 80
    assert metrics.playout_queue_peak_ms == 360
    assert metrics.pacer_underrun_count == 2
    assert metrics.pacer_partial_pad_count == 1
    assert metrics.server_playout_drained_latency_ms == 900

    summary_update = critical[1].update  # type: ignore[attr-defined]
    assert summary_update.turn_count == 1
    assert summary_update.interruption_count == 1
    assert summary_update.summary.playout_queue_peak_ms == 360
    assert summary_update.summary.failed_turn_count == 0

    await recorder.upsert_turn_metrics(
        session_id="rt_abc123",
        generation_id=2,
        metrics={
            "outcome": "failed",
            "failure_stage": "tts",
            "error_code": "tts_upstream_error",
            "peak_buffered_ms": 240,
        },
    )
    second_summary = critical[3].update  # type: ignore[attr-defined]
    assert second_summary.turn_count == 2
    assert second_summary.interruption_count == 1
    assert second_summary.summary.playout_queue_peak_ms == 360
    assert second_summary.summary.failed_turn_count == 1


@pytest.mark.asyncio
async def test_critical_operations_wait_instead_of_dropping(monkeypatch) -> None:
    recorder = RealtimeTelemetryRecorder(
        observation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        runtime_session_id="rt_abc123",
        queue_capacity=1,
    )
    monkeypatch.setattr(recorder, "start", lambda: None)
    recorder._queue.put_nowait(SimpleNamespace(kind="event"))  # type: ignore[arg-type]

    pending = asyncio.create_task(recorder.mark_connected(session_id="rt_abc123"))
    await asyncio.sleep(0)
    assert not pending.done()
    recorder._queue.get_nowait()
    recorder._queue.task_done()
    await pending
    assert recorder.dropped_count == 0

    recorder._queue.get_nowait()
    recorder._queue.task_done()


@pytest.mark.asyncio
async def test_critical_persistence_retries_past_best_effort_limit(monkeypatch) -> None:
    recorder = RealtimeTelemetryRecorder(
        observation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        runtime_session_id="rt_abc123",
    )
    attempts = 0

    async def _persist(_batch: list[object]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise ConnectionError

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(recorder, "_persist_batch", _persist)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    await recorder._persist_with_retry([SimpleNamespace(kind="critical")])  # type: ignore[list-item]

    assert attempts == 4
    assert recorder.dropped_count == 0


def test_late_connected_update_cannot_regress_terminal_state() -> None:
    session = SimpleNamespace(
        status="completed",
        connected_at=None,
        ended_at=None,
        failure_stage=None,
        error_code=None,
        duration_ms=100,
        turn_count=1,
        interruption_count=0,
        summary={},
        updated_at=None,
    )

    RealtimeTelemetryRecorder._apply_session_update(
        session,
        RealtimeTelemetrySessionUpdateRequest(status="connected"),
    )

    assert session.status == "completed"
    assert session.connected_at is None


def test_factory_creates_recorder_with_preclaimed_runtime_id() -> None:
    recorder = RealtimeTelemetryRecorderFactory().create(
        observation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        runtime_session_id="rt_deadbeef",
    )

    assert recorder.runtime_session_id == "rt_deadbeef"
