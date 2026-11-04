"""Non-blocking persistence sink for realtime session metrics."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from src.db.session import AsyncSessionLocal
from src.realtime.session.metrics import (
    MetricPayload,
    RealtimeSessionMetricsSink,
    sanitize_metric_context,
    sanitize_metric_payload,
)
from src.repositories.realtime_telemetry_repository import RealtimeTelemetryRepository
from src.schemas.realtime_telemetry import (
    MAX_BATCH_SIZE,
    RealtimeSessionSummary,
    RealtimeTelemetryEventInput,
    RealtimeTelemetrySessionUpdateRequest,
    RealtimeTelemetryStatus,
    RealtimeTurnMetricsWrite,
    sanitize_event_payload,
    validate_rtc_session_id,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _EventOperation:
    event: RealtimeTelemetryEventInput


@dataclass(frozen=True, slots=True)
class _TurnOperation:
    metrics: RealtimeTurnMetricsWrite


@dataclass(frozen=True, slots=True)
class _SessionOperation:
    update: RealtimeTelemetrySessionUpdateRequest


@dataclass(frozen=True, slots=True)
class _AttachOperation:
    pass


_Operation = _EventOperation | _TurnOperation | _SessionOperation | _AttachOperation


class RealtimeTelemetryRecorder(RealtimeSessionMetricsSink):
    """Map one internal rt_* session to one observation UUID and persist off-path.

    The protocol methods only validate and call ``Queue.put_nowait``. All database
    I/O runs in the background task, never on the 20 ms media path.
    """

    def __init__(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        runtime_session_id: str | None = None,
        queue_capacity: int = 1_000,
        batch_size: int = MAX_BATCH_SIZE,
        flush_interval_seconds: float = 0.1,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
        if flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")
        self.observation_id = observation_id
        self.user_id = user_id
        self.runtime_session_id = (
            validate_rtc_session_id(runtime_session_id)
            if runtime_session_id is not None
            else None
        )
        self._queue: asyncio.Queue[_Operation] = asyncio.Queue(maxsize=queue_capacity)
        self._batch_size = batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._started_monotonic = time.monotonic()
        self._server_seq = 0
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._dropped_count = 0
        self._persisted_count = 0
        self._generation_metrics: dict[int, dict[str, int | str]] = {}
        self._generation_offsets: dict[int, dict[str, int]] = {}
        self._last_speech_stop_offset_ms: int | None = None
        self._turn_count = 0
        self._interruption_count = 0
        self._failed_turn_count = 0
        self._playout_queue_peak_ms = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._run(),
                name=f"realtime-telemetry-{self.observation_id}",
            )

    async def attach_runtime_session(self, runtime_session_id: str) -> None:
        if self._closing:
            raise RuntimeError("telemetry recorder is closing")
        normalized = validate_rtc_session_id(runtime_session_id)
        if self.runtime_session_id not in {None, normalized}:
            raise ValueError("telemetry recorder is already attached to another runtime session")
        self.runtime_session_id = normalized
        await self._enqueue_critical(_AttachOperation())

    async def record_event(
        self,
        *,
        session_id: str,
        event_type: str,
        generation_id: int | None = None,
        stage: str | None = None,
        reason_code: str | None = None,
        metrics: MetricPayload | None = None,
    ) -> None:
        self._verify_runtime_session(session_id)
        event_type, stage, reason_code = sanitize_metric_context(
            event_type=event_type,
            stage=stage,
            reason_code=reason_code,
        )
        payload = dict(sanitize_metric_payload(metrics))
        offset_value = payload.pop("offset_ms", None)
        if offset_value is None:
            occurred_offset_ms = max(
                0,
                int((time.monotonic() - self._started_monotonic) * 1_000),
            )
        elif (
            isinstance(offset_value, bool)
            or not isinstance(offset_value, (int, float))
            or offset_value < 0
        ):
            raise ValueError("offset_ms must be non-negative numeric")
        else:
            occurred_offset_ms = int(offset_value)
        if "from_state" in payload:
            if "previous_state" in payload:
                raise ValueError("from_state and previous_state cannot both be provided")
            payload["previous_state"] = payload.pop("from_state")
        if "to_state" in payload:
            if "state" in payload:
                raise ValueError("to_state and state cannot both be provided")
            payload["state"] = payload.pop("to_state")
        if generation_id is not None:
            if generation_id < 0:
                raise ValueError("generation_id must be non-negative")
            payload["generation_id"] = generation_id
        payload = sanitize_event_payload(payload)
        event = RealtimeTelemetryEventInput(
            seq=self._server_seq,
            event_type=event_type,
            stage=stage,
            reason_code=reason_code,
            occurred_offset_ms=occurred_offset_ms,
            payload=payload,
        )
        self._accumulate_event_metrics(event, generation_id)
        self._server_seq += 1
        self._enqueue(_EventOperation(event=event))

    async def upsert_turn_metrics(
        self,
        *,
        session_id: str,
        generation_id: int,
        turn_id: uuid.UUID | None = None,
        metrics: MetricPayload,
    ) -> None:
        del turn_id  # Turn UUID is not required for content-free aggregate metrics.
        self._verify_runtime_session(session_id)
        if generation_id < 0:
            raise ValueError("generation_id must be non-negative")
        payload = dict(sanitize_metric_payload(metrics))
        turn_seq_value = payload.pop("turn_seq", generation_id)
        if isinstance(turn_seq_value, bool) or not isinstance(turn_seq_value, int):
            raise ValueError("turn_seq must be an integer")
        runtime_names = {
            "buffered_ms": "playout_queue_final_ms",
            "peak_buffered_ms": "playout_queue_peak_ms",
            "underrun_count": "pacer_underrun_count",
            "partial_pad_count": "pacer_partial_pad_count",
            "frames_read": "pacer_frames_read",
            "silence_frames": "pacer_silence_frames",
            "rejected_write_count": "pacer_rejected_write_count",
            "fadeout_count": "pacer_fadeout_count",
            "first_audio_enqueued_ms": "first_audio_enqueued_latency_ms",
            "server_first_audio_read_ms": "server_first_audio_read_latency_ms",
            "server_rtp_source_drained_ms": "server_playout_drained_latency_ms",
        }
        for runtime_name, persisted_name in runtime_names.items():
            if runtime_name not in payload:
                continue
            if persisted_name in payload:
                raise ValueError(f"{runtime_name} and {persisted_name} cannot both be provided")
            payload[persisted_name] = payload.pop(runtime_name)
        allowed = set(RealtimeTurnMetricsWrite.model_fields) - {"generation_id", "turn_seq"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown turn metric: {sorted(unknown)[0]}")
        combined_metrics = {
            **self._generation_metrics.pop(generation_id, {}),
            **payload,
        }
        self._generation_offsets.pop(generation_id, None)
        model = RealtimeTurnMetricsWrite(
            generation_id=generation_id,
            turn_seq=turn_seq_value,
            **combined_metrics,
        )
        self._turn_count += 1
        if model.outcome.value == "interrupted":
            self._interruption_count += 1
        elif model.outcome.value == "failed":
            self._failed_turn_count += 1
        if model.playout_queue_peak_ms is not None:
            self._playout_queue_peak_ms = max(
                self._playout_queue_peak_ms,
                model.playout_queue_peak_ms,
            )
        await self._enqueue_critical(_TurnOperation(metrics=model))
        await self._enqueue_critical(
            _SessionOperation(
                update=RealtimeTelemetrySessionUpdateRequest(
                    turn_count=self._turn_count,
                    interruption_count=self._interruption_count,
                    summary=RealtimeSessionSummary(
                        turn_count=self._turn_count,
                        interruption_count=self._interruption_count,
                        failed_turn_count=self._failed_turn_count,
                        playout_queue_peak_ms=self._playout_queue_peak_ms,
                    ),
                )
            )
        )

    async def mark_connected(self, *, session_id: str) -> None:
        self._verify_runtime_session(session_id)
        await self._enqueue_critical(
            _SessionOperation(
                update=RealtimeTelemetrySessionUpdateRequest(
                    status=RealtimeTelemetryStatus.CONNECTED
                )
            )
        )

    async def finalize(
        self,
        *,
        session_id: str,
        status: Literal["completed", "failed", "cancelled"],
        duration_ms: int | None = None,
        turn_count: int | None = None,
        interruption_count: int | None = None,
        failure_stage: str | None = None,
        error_code: str | None = None,
        summary: RealtimeSessionSummary | None = None,
    ) -> None:
        self._verify_runtime_session(session_id)
        update = RealtimeTelemetrySessionUpdateRequest(
            status=status,
            duration_ms=duration_ms,
            turn_count=turn_count,
            interruption_count=interruption_count,
            failure_stage=failure_stage,
            error_code=error_code,
            summary=summary,
        )
        await self._enqueue_critical(_SessionOperation(update=update))

    async def finalize_failed(
        self,
        *,
        failure_stage: str,
        error_code: str,
        duration_ms: int | None = None,
    ) -> None:
        """Persist a failed pre-negotiation attempt before an rt_* id is attached."""

        update = RealtimeTelemetrySessionUpdateRequest(
            status=RealtimeTelemetryStatus.FAILED,
            failure_stage=failure_stage,
            error_code=error_code,
            duration_ms=duration_ms,
        )
        await self._enqueue_critical(_SessionOperation(update=update))

    async def close(self, *, timeout_seconds: float = 5.0) -> None:
        if self._dropped_count:
            await self._enqueue_critical(
                _SessionOperation(
                    update=RealtimeTelemetrySessionUpdateRequest(
                        summary=RealtimeSessionSummary(
                            dropped_event_count=self._dropped_count
                        )
                    )
                )
            )
        self._closing = True
        self.start()
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_seconds)
            if self._worker is not None:
                await asyncio.wait_for(self._worker, timeout=self._flush_interval_seconds * 2 + 1)
        except TimeoutError:
            logger.error(
                "event=realtime.telemetry_close_timeout outcome=degraded "
                "observation_id=%s queued=%s dropped=%s",
                self.observation_id,
                self._queue.qsize(),
                self._dropped_count,
            )
            if self._worker is not None:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
        else:
            logger.info(
                "event=realtime.telemetry_writer_completed outcome=succeeded "
                "observation_id=%s persisted=%s dropped=%s",
                self.observation_id,
                self._persisted_count,
                self._dropped_count,
            )

    def _verify_runtime_session(self, session_id: str) -> None:
        if self.runtime_session_id is None or session_id != self.runtime_session_id:
            raise ValueError("runtime session does not match telemetry recorder")
        if self._closing:
            raise RuntimeError("telemetry recorder is closing")

    def _enqueue(self, operation: _Operation) -> None:
        self.start()
        try:
            self._queue.put_nowait(operation)
        except asyncio.QueueFull:
            self._dropped_count += 1
            logger.warning(
                "event=realtime.telemetry_queue_full outcome=dropped "
                "observation_id=%s dropped=%s capacity=%s",
                self.observation_id,
                self._dropped_count,
                self._queue.maxsize,
            )

    async def _enqueue_critical(self, operation: _Operation) -> None:
        if self._closing:
            raise RuntimeError("telemetry recorder is closing")
        self.start()
        await self._queue.put(operation)

    async def _run(self) -> None:
        while not self._closing or not self._queue.empty():
            try:
                first = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=self._flush_interval_seconds,
                )
            except TimeoutError:
                continue
            batch = [first]
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._persist_with_retry(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _accumulate_event_metrics(
        self,
        event: RealtimeTelemetryEventInput,
        generation_id: int | None,
    ) -> None:
        if event.event_type == "speech_stop":
            self._last_speech_stop_offset_ms = event.occurred_offset_ms
        if generation_id is None:
            return
        metrics = self._generation_metrics.setdefault(generation_id, {})
        offsets = self._generation_offsets.setdefault(generation_id, {})
        if event.event_type == "end_of_turn_confirmed":
            utterance_ms = event.payload.get("utterance_ms")
            if isinstance(utterance_ms, (int, float)) and not isinstance(utterance_ms, bool):
                metrics["speech_duration_ms"] = int(utterance_ms)
            if self._last_speech_stop_offset_ms is not None:
                metrics["endpointing_latency_ms"] = max(
                    0,
                    event.occurred_offset_ms - self._last_speech_stop_offset_ms,
                )
            offsets["end_of_turn"] = event.occurred_offset_ms
        elif event.event_type == "stt_final":
            latency_ms = event.payload.get("latency_ms")
            if isinstance(latency_ms, (int, float)) and not isinstance(latency_ms, bool):
                metrics["stt_final_latency_ms"] = int(latency_ms)
        elif event.event_type == "llm_request":
            offsets["llm_request"] = event.occurred_offset_ms
        elif event.event_type == "llm_first_token":
            latency_ms = event.payload.get("latency_ms")
            if isinstance(latency_ms, (int, float)) and not isinstance(latency_ms, bool):
                metrics["llm_ttft_ms"] = int(latency_ms)
            elif "llm_request" in offsets:
                metrics["llm_ttft_ms"] = max(
                    0,
                    event.occurred_offset_ms - offsets["llm_request"],
                )
        elif event.event_type == "first_speakable_chunk" and "end_of_turn" in offsets:
            metrics["first_speakable_chunk_latency_ms"] = max(
                0,
                event.occurred_offset_ms - offsets["end_of_turn"],
            )
        elif event.event_type == "tts_request_start":
            offsets["tts_request_start"] = event.occurred_offset_ms
        elif event.event_type == "tts_first_audio" and "tts_request_start" in offsets:
            metrics["tts_ttfb_ms"] = max(
                0,
                event.occurred_offset_ms - offsets["tts_request_start"],
            )
        elif event.event_type == "failure":
            metrics["outcome"] = "failed"
            if event.stage is not None:
                metrics["failure_stage"] = event.stage.value
            if event.reason_code is not None:
                metrics["error_code"] = event.reason_code

    async def _persist_with_retry(self, batch: list[_Operation]) -> None:
        has_critical_operation = any(
            not isinstance(operation, _EventOperation) for operation in batch
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._persist_batch(batch)
            except Exception as exc:
                if not has_critical_operation and attempt >= 3:
                    self._dropped_count += len(batch)
                    logger.error(
                        "event=realtime.telemetry_batch_failed outcome=dropped "
                        "observation_id=%s batch_size=%s attempts=%s error_type=%s",
                        self.observation_id,
                        len(batch),
                        attempt,
                        type(exc).__name__,
                    )
                    return
                logger.warning(
                    "event=realtime.telemetry_batch_retry outcome=retrying "
                    "observation_id=%s batch_size=%s attempt=%s critical=%s error_type=%s",
                    self.observation_id,
                    len(batch),
                    attempt,
                    has_critical_operation,
                    type(exc).__name__,
                )
                await asyncio.sleep(min(0.5, 0.05 * (2 ** min(attempt - 1, 4))))
            else:
                self._persisted_count += len(batch)
                return

    async def _persist_batch(self, batch: list[_Operation]) -> None:
        async with AsyncSessionLocal() as db:
            db.info["current_user_id"] = str(self.user_id)
            repo = RealtimeTelemetryRepository(db)
            async with db.begin():
                session = await repo.get_session(
                    observation_id=self.observation_id,
                    user_id=self.user_id,
                    for_update=True,
                )
                if session is None:
                    raise RuntimeError("telemetry session no longer exists")
                if self.runtime_session_id is not None:
                    if session.rtc_session_id is None:
                        session.rtc_session_id = self.runtime_session_id
                    elif session.rtc_session_id != self.runtime_session_id:
                        raise RuntimeError("persisted runtime session association does not match")

                events: list[dict[str, object]] = []
                for operation in batch:
                    if isinstance(operation, _EventOperation):
                        events.append(
                            {
                                "observation_id": self.observation_id,
                                "user_id": self.user_id,
                                "source": "server",
                                **operation.event.model_dump(mode="json"),
                            }
                        )
                    elif isinstance(operation, _TurnOperation):
                        await repo.upsert_turn_metrics(
                            {
                                "observation_id": self.observation_id,
                                "user_id": self.user_id,
                                **operation.metrics.model_dump(exclude_none=True, mode="json"),
                            }
                        )
                    elif isinstance(operation, _SessionOperation):
                        self._apply_session_update(session, operation.update)
                await repo.insert_events(events)

    @staticmethod
    def _apply_session_update(session: object, update: RealtimeTelemetrySessionUpdateRequest) -> None:
        now = datetime.now(UTC)
        status = update.status
        status_compatible = True
        if status is not None:
            terminal = {"completed", "failed", "cancelled"}
            current_status = str(session.status)
            if current_status not in terminal:
                session.status = status.value
                if status == RealtimeTelemetryStatus.CONNECTED:
                    session.connected_at = session.connected_at or now
                elif status.value in terminal:
                    session.ended_at = session.ended_at or now
            elif current_status != status.value:
                status_compatible = False
        for name in (
            "failure_stage",
            "error_code",
            "duration_ms",
            "turn_count",
            "interruption_count",
        ):
            if name not in update.model_fields_set:
                continue
            value = getattr(update, name)
            if name in {"failure_stage", "error_code"}:
                if not status_compatible or getattr(session, name) is not None:
                    continue
                setattr(session, name, value.value if hasattr(value, "value") else value)
                continue
            if value is not None:
                setattr(session, name, max(getattr(session, name) or 0, value))
        if update.summary is not None:
            session.summary = {
                **session.summary,
                **update.summary.model_dump(exclude_unset=True, exclude_none=True),
            }
        session.updated_at = now


class RealtimeTelemetryRecorderFactory:
    """Create an unbound, per-call recorder for router/session-manager wiring."""

    def create(
        self,
        *,
        observation_id: uuid.UUID,
        user_id: uuid.UUID,
        runtime_session_id: str | None = None,
    ) -> RealtimeTelemetryRecorder:
        return RealtimeTelemetryRecorder(
            observation_id=observation_id,
            user_id=user_id,
            runtime_session_id=runtime_session_id,
        )


__all__ = ["RealtimeTelemetryRecorder", "RealtimeTelemetryRecorderFactory"]
