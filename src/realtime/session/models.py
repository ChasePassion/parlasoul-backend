"""Realtime session runtime models."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, TypeVar

import numpy as np

from src.core.config import settings
from src.realtime.bridge.event_bridge import EventBridge
from src.realtime.bridge.subtitle_bridge import SubtitleBridge
from src.realtime.pipeline.audio_pacer import AudioPacer, AudioPacerStats
from src.realtime.pipeline.bus import RealtimeEventBus
from src.realtime.pipeline.events import RealtimeEvent
from src.realtime.pipeline.interrupt_manager import InterruptManager
from src.realtime.pipeline.turn_commit_gate import (
    CommitReason,
    DiscardReason,
    TurnCommitGate,
    TurnCommitGateConfig,
)
from src.realtime.rtc.input_track import InputTrackReader, resample_pcm16
from src.realtime.rtc.peer import RealtimePeer
from src.realtime.schemas.client_events import (
    InputAudioBufferMuteEvent,
    InputAudioBufferUnmuteEvent,
    ResponseCancelEvent,
    SessionPingEvent,
    SessionUpdateEvent,
)
from src.realtime.session.metrics import (
    MetricPayload,
    RealtimeSessionMetricsSink,
    sanitize_metric_context,
    sanitize_metric_payload,
)
from src.realtime.session.state import RealtimeSessionState

logger = logging.getLogger(__name__)
_TaskResult = TypeVar("_TaskResult")


@dataclass(slots=True)
class CurrentTurnContext:
    history_leaf_turn_id: uuid.UUID | None
    user_turn_id: uuid.UUID
    user_candidate_id: uuid.UUID
    assistant_turn_id: uuid.UUID
    assistant_candidate_id: uuid.UUID
    raw_user_text: str
    assistant_accumulated_text: str = ""
    llm_completed: bool = False
    interrupted: bool = False
    generation_id: int = 0


@dataclass(slots=True)
class SessionConfig:
    translation_enabled: bool = True
    turn_commit: TurnCommitGateConfig = field(default_factory=TurnCommitGateConfig)
    interrupt_hard_confirm_ms: int = 300
    interrupt_resume_grace_ms: int = 250
    # 0 disables backchannel tolerance (produce the pre-2026 interrupt behavior).
    backchannel_max_voiced_ms: int = 0
    backchannel_max_wait_ms: int = 500
    backchannel_max_watch_ms: int = 3000


class RealtimeSession:
    def __init__(
        self,
        *,
        session_id: str,
        user_id: uuid.UUID,
        chat_id: uuid.UUID,
        character_id: uuid.UUID,
        peer: RealtimePeer,
        bus: RealtimeEventBus,
        audio_pacer: AudioPacer,
        event_bridge: EventBridge,
        subtitle_bridge: SubtitleBridge,
        interrupt_manager: InterruptManager,
        dialog_service,
        turn_service,
        voice_runtime,
        config: SessionConfig,
        metrics_sink: RealtimeSessionMetricsSink | None = None,
    ) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.chat_id = chat_id
        self.character_id = character_id
        self.peer = peer
        self.bus = bus
        self.audio_pacer = audio_pacer
        self.event_bridge = event_bridge
        self.subtitle_bridge = subtitle_bridge
        self.interrupt_manager = interrupt_manager
        self.dialog_service = dialog_service
        self.turn_service = turn_service
        self.voice_runtime = voice_runtime
        self.config = config
        self._metrics_sink = metrics_sink

        self._started_monotonic = time.monotonic()
        self._state = RealtimeSessionState.LISTENING
        self._turn_gate = TurnCommitGate(
            config=config.turn_commit,
            on_commit=self._on_utterance_commit,
            on_discard=self._on_utterance_discard,
            session_id=session_id,
            chat_id=str(chat_id),
            character_id=str(character_id),
        )
        self._response_task: asyncio.Task[None] | None = None
        self._soft_interrupt_task: asyncio.Task[None] | None = None
        self._resume_task: asyncio.Task[None] | None = None
        self._resume_token = 0
        self._soft_interrupt_active = False
        self._speech_active = False
        self._current_cancel_event: asyncio.Event | None = None
        self._current_turn_ctx: CurrentTurnContext | None = None
        self._generation_counter = 0
        self._commit_request_counter = 0
        self._latest_commit_request_id = 0
        self._current_generation_id: int | None = None
        self._cancel_reasons: dict[int, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._generation_tasks: dict[int, set[asyncio.Task[Any]]] = {}
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._generation_lock = asyncio.Lock()
        self._input_reader: InputTrackReader | None = None
        self._track = None  # saved for unmute restart
        self._stt_stream = None

        bind_state = getattr(self.interrupt_manager, "bind_state_controller", None)
        if callable(bind_state):
            bind_state(getter=lambda: self.state, transition=self._transition_state)

    @property
    def state(self) -> RealtimeSessionState:
        return self._state

    @state.setter
    def state(self, value: RealtimeSessionState) -> None:
        # Compatibility for tests and callers which previously assigned state.
        self._state = RealtimeSessionState(value)

    @property
    def current_generation_id(self) -> int | None:
        return self._current_generation_id

    async def start(self) -> None:
        await self.event_bridge.start()
        await self.peer.send_json(
            {
                "type": "session.created",
                "session_id": self.session_id,
                "chat_id": str(self.chat_id),
                "character_id": str(self.character_id),
            }
        )
        await self._record_metric("session_started", stage="session")
        self._spawn(self._warm_stt_stream(), name=f"realtime.stt_warm.{self.session_id}")

    async def _warm_stt_stream(self) -> None:
        """Pre-open a progressive ASR session so end-of-turn only decodes the tail."""
        self._ensure_stt_stream()

    def _ensure_stt_stream(self):
        """Return a usable stream, creating/starting a fresh one when needed.

        Upstream realtime ASR sessions go stale while a long reply is playing,
        so a stream older than ``REALTIME_STT_STREAM_MAX_AGE_SECONDS`` is
        replaced before the next utterance instead of being reused.
        """
        if not settings.REALTIME_STT_STREAMING_ENABLED or self._closed:
            return None
        max_age = max(1.0, float(settings.REALTIME_STT_STREAM_MAX_AGE_SECONDS))
        stream = self._stt_stream
        if stream is not None and getattr(stream, "usable", True) and stream.age_seconds <= max_age:
            return stream
        if stream is not None:
            self._stt_stream = None
            self._spawn(
                stream.abort(),
                name=f"realtime.stt_abort_stale.{self.session_id}",
            )
        try:
            fresh = self.voice_runtime.open_streaming_stt()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Realtime streaming STT unavailable, using one-shot path: session_id=%s "
                "error_type=%s",
                self.session_id,
                type(exc).__name__,
            )
            return None
        if fresh is None:
            return None
        self._stt_stream = fresh
        self._spawn(
            self._start_stt_stream(fresh),
            name=f"realtime.stt_start.{self.session_id}",
        )
        return fresh

    async def _start_stt_stream(self, stream) -> None:
        try:
            await stream.start()
            logger.info(
                "Realtime streaming STT warmed: session_id=%s",
                self.session_id,
                extra={"event": "realtime.stt.stream_warmed"},
            )
        except asyncio.CancelledError:
            await stream.abort()
            if self._stt_stream is stream:
                self._stt_stream = None
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Realtime streaming STT start failed, using one-shot path: session_id=%s "
                "error_type=%s",
                self.session_id,
                type(exc).__name__,
            )
            await stream.abort()
            if self._stt_stream is stream:
                self._stt_stream = None

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._close_resources()

    async def _close_resources(self) -> None:
        await self._turn_gate.close()
        if self._stt_stream is not None:
            stream, self._stt_stream = self._stt_stream, None
            await stream.abort()
        await self._cancel_soft_interrupt(resume=False, reason="session_closed")
        await self.cancel_response("session_closed", clear_audio=False)

        current = asyncio.current_task()
        remaining = [task for task in self._tasks if task is not current and not task.done()]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)

        if self._input_reader:
            await self._input_reader.stop()
            self._input_reader = None
        await self.event_bridge.stop()
        await self.audio_pacer.close()
        await self.peer.close()
        close_sink = getattr(self._metrics_sink, "close", None)
        if callable(close_sink):
            try:
                result = close_sink()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning(
                    "Realtime metrics sink close failed: session_id=%s error_type=%s",
                    self.session_id,
                    type(exc).__name__,
                )

    async def attach_audio_track(self, track) -> None:
        if self._closed:
            return
        if self._input_reader is not None:
            await self._input_reader.stop()
        self._track = track
        self._input_reader = InputTrackReader(
            track=track,
            on_pcm16_frame=self.process_audio_frame,
        )
        self._input_reader.start()

    async def mark_metrics_connected(self) -> None:
        mark_connected = getattr(self._metrics_sink, "mark_connected", None)
        if not callable(mark_connected):
            return
        try:
            result = mark_connected(session_id=self.session_id)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "Realtime metrics connected marker failed: session_id=%s error_type=%s",
                self.session_id,
                type(exc).__name__,
            )

    async def finalize_metrics(
        self,
        *,
        status: str,
        failure_stage: str | None = None,
        error_code: str | None = None,
    ) -> None:
        finalize = getattr(self._metrics_sink, "finalize", None)
        if not callable(finalize):
            return
        try:
            result = finalize(
                session_id=self.session_id,
                status=status,
                duration_ms=self._session_offset_ms(),
                failure_stage=failure_stage,
                error_code=error_code,
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "Realtime metrics terminal marker failed: session_id=%s status=%s error_type=%s",
                self.session_id,
                status,
                type(exc).__name__,
            )

    async def process_audio_frame(self, pcm16: bytes, sample_rate: int) -> None:
        if self._closed:
            return
        samples = np.frombuffer(pcm16, dtype=np.int16)
        if sample_rate != 16000:
            samples = resample_pcm16(samples, sample_rate, 16000)
        frame_bytes = samples.tobytes()
        state_before = self.state
        resume_pending_before_frame = self._resume_task is not None
        transition = await self.interrupt_manager.ingest_frame(frame_bytes, frame_ms=20)

        if transition.speech_started:
            resume_pending = resume_pending_before_frame or self._resume_task is not None
            await self._cancel_pending_resume()
            self._speech_active = True
            self._turn_gate.reset_with_preroll(transition.preroll_audio)
            self._turn_gate.mark_voiced(transition.voiced_ms_delta)
            stream = self._ensure_stt_stream()
            if stream is not None:
                if stream.appended_audio_ms == 0:
                    # preroll only for the first speech segment of this utterance
                    stream.append(transition.preroll_audio)
                stream.append(frame_bytes)
            await self._record_metric(
                "speech_start",
                generation_id=self._current_generation_id,
                stage="vad",
                metrics={
                    "confidence": float(transition.confidence),
                    "offset_ms": self._session_offset_ms(),
                },
            )
            if state_before == RealtimeSessionState.AGENT_SPEAKING or resume_pending:
                await self._begin_soft_interrupt("user_speech_detected")
            elif state_before == RealtimeSessionState.THINKING and self._response_task is not None:
                self._spawn(
                    self.cancel_response(
                        "user_speech_while_thinking",
                        clear_audio=True,
                    ),
                    name=f"realtime.thinking_interrupt.{self.session_id}",
                )
        else:
            if (
                self.state == RealtimeSessionState.USER_SPEAKING
                or self.interrupt_manager.state == RealtimeSessionState.USER_SPEAKING
            ):
                self._turn_gate.append(frame_bytes)
            self._turn_gate.mark_voiced(transition.voiced_ms_delta)
            if self.state == RealtimeSessionState.USER_SPEAKING:
                self._stt_stream_append(frame_bytes)

        if transition.speech_stopped:
            self._speech_active = False
            await self._record_metric(
                "speech_stop",
                generation_id=self._current_generation_id,
                stage="vad",
                metrics={
                    "confidence": float(transition.confidence),
                    "voiced_ms": self._turn_gate.voiced_ms,
                    "offset_ms": self._session_offset_ms(),
                },
            )
            if self._soft_interrupt_active:
                await self._cancel_soft_interrupt(
                    resume=True,
                    reason="speech_stopped_before_confirm",
                )
                await self._turn_gate.discard("cancelled")
                return
            await self._transition_state(
                RealtimeSessionState.ENDPOINT_PENDING,
                "vad_speech_stopped",
            )
            self._turn_gate.schedule_commit("vad_silence")

    def _stt_stream_append(self, pcm16: bytes) -> None:
        stream = self._stt_stream
        if stream is not None:
            stream.append(pcm16)

    async def handle_client_event(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        if event_type == "session.update":
            event = SessionUpdateEvent.model_validate(payload)
            if event.translation_enabled is not None:
                self.config.translation_enabled = event.translation_enabled
            await self.bus.publish(
                RealtimeEvent(
                    "session_updated",
                    {"translation_enabled": self.config.translation_enabled},
                )
            )
            return
        if event_type == "session.ping":
            SessionPingEvent.model_validate(payload)
            return
        if event_type == "response.cancel":
            ResponseCancelEvent.model_validate(payload)
            generation_id = self._current_generation_id
            await self.interrupt_manager.maybe_interrupt(
                reason="client_response_cancelled",
                generation_id=generation_id,
            )
            await self.cancel_response("client_response_cancelled", clear_audio=True)
            return
        if event_type == "input_audio_buffer.mute":
            InputAudioBufferMuteEvent.model_validate(payload)
            if self.state == RealtimeSessionState.USER_SPEAKING or self._turn_gate.has_pending_commit:
                await self._cancel_soft_interrupt(resume=False, reason="mic_muted")
                self._speech_active = False
                await self._transition_state(
                    RealtimeSessionState.ENDPOINT_PENDING,
                    "mic_muted",
                )
                await self.bus.publish(
                    RealtimeEvent(
                        "speech_stopped",
                        {
                            "at_ms": int(time.time() * 1000),
                            "audio_ms": self._turn_gate.audio_ms,
                            "confidence": 0.0,
                            "reason": "mic_muted",
                        },
                    )
                )
                await self._turn_gate.force_commit("manual_mute")
            if self._input_reader:
                await self._input_reader.stop()
                self._input_reader = None
            logger.info("Realtime mic muted: session_id=%s", self.session_id)
            return
        if event_type == "input_audio_buffer.unmute":
            InputAudioBufferUnmuteEvent.model_validate(payload)
            if self._track and self._input_reader is None and not self._closed:
                self._input_reader = InputTrackReader(
                    track=self._track,
                    on_pcm16_frame=self.process_audio_frame,
                )
                self._input_reader.start()
            logger.info("Realtime mic unmuted: session_id=%s", self.session_id)
            return
        await self.bus.publish(
            RealtimeEvent(
                "error",
                {"code": "invalid_param", "message": f"unsupported client event: {event_type}"},
            )
        )

    async def cancel_response(self, reason: str, *, clear_audio: bool = True) -> None:
        await self._cancel_soft_interrupt(resume=False, reason=reason)
        generation_id = self._current_generation_id
        if generation_id is not None:
            self._cancel_reasons.setdefault(generation_id, reason)
        cancel_event = self._current_cancel_event
        if cancel_event is not None:
            cancel_event.set()
        if self._current_turn_ctx is not None:
            self._current_turn_ctx.interrupted = self._is_barge_in_reason(reason)

        fade_applied = False
        if clear_audio and generation_id is not None:
            fade_applied = await self.audio_pacer.clear_with_fadeout(generation_id)
            if fade_applied:
                self._spawn(
                    self._wait_for_audio_stopped(generation_id, reason),
                    name=f"realtime.audio_stop.{self.session_id}.{generation_id}",
                )

        current = asyncio.current_task()
        tasks: set[asyncio.Task[Any]] = set()
        if self._response_task is not None and self._response_task is not current:
            tasks.add(self._response_task)
        if generation_id is not None:
            tasks.update(
                task
                for task in self._generation_tasks.get(generation_id, set())
                if task is not current
            )
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if self._response_task in tasks and self._response_task is not None:
            if self._response_task.done():
                self._response_task = None
        logger.info(
            "Realtime response cancelled: session_id=%s generation_id=%s reason=%s fade_applied=%s",
            self.session_id,
            generation_id,
            reason,
            fade_applied,
            extra={"event": "realtime.response.cancelled"},
        )

    async def _on_utterance_commit(self, utterance: bytes, reason: CommitReason) -> None:
        if self._closed:
            return
        self._commit_request_counter += 1
        request_id = self._commit_request_counter
        self._latest_commit_request_id = request_id

        previous_generation = self._current_generation_id
        previous_tasks = (
            self._generation_tasks.get(previous_generation, set())
            if previous_generation is not None
            else set()
        )
        if (
            self._response_task is not None
            and not self._response_task.done()
        ) or any(not task.done() for task in previous_tasks):
            await self.cancel_response(
                "user_utterance_committed",
                clear_audio=self.state == RealtimeSessionState.AGENT_SPEAKING,
            )

        async with self._generation_lock:
            if self._closed or request_id != self._latest_commit_request_id:
                return
            self._generation_counter += 1
            generation_id = self._generation_counter
            self._current_generation_id = generation_id
            cancel_event = asyncio.Event()
            self._current_cancel_event = cancel_event
            start_event = asyncio.Event()
            task = self._spawn(
                self._run_response_after_start(
                    start_event,
                    utterance,
                    generation_id,
                    cancel_event,
                ),
                name=f"realtime.response.{self.session_id}.{generation_id}",
                generation_id=generation_id,
            )
            self._response_task = task
            task.add_done_callback(self._clear_response_task)

        await self._transition_state(
            RealtimeSessionState.THINKING,
            "end_of_turn_confirmed",
        )
        audio_ms = int(
            len(utterance)
            / self.config.turn_commit.bytes_per_sample
            / self.config.turn_commit.sample_rate
            * 1000
        )
        await self._record_metric(
            "end_of_turn_confirmed",
            generation_id=generation_id,
            stage="endpointing",
            reason_code=reason,
            metrics={
                "utterance_ms": audio_ms,
                "voiced_ms": self._turn_gate.last_voiced_ms,
                "offset_ms": self._session_offset_ms(),
            },
        )
        if (
            request_id == self._latest_commit_request_id
            and self._owns_generation(generation_id)
            and not task.done()
        ):
            start_event.set()

        logger.info(
            "Realtime utterance committed: session_id=%s generation_id=%s reason=%s audio_ms=%s voiced_ms=%s",
            self.session_id,
            generation_id,
            reason,
            audio_ms,
            self._turn_gate.last_voiced_ms,
            extra={
                "event": "realtime.utterance.committed",
                "chat_id": str(self.chat_id),
                "character_id": str(self.character_id),
            },
        )

    async def _on_utterance_discard(self, reason: DiscardReason, audio_ms: int) -> None:
        resume_pending = self._resume_task is not None and not self._resume_task.done()
        if self._stt_stream is not None:
            stream, self._stt_stream = self._stt_stream, None
            await stream.abort()
            self._spawn(self._warm_stt_stream(), name=f"realtime.stt_warm.{self.session_id}")
        if self.state != RealtimeSessionState.AGENT_SPEAKING and not resume_pending:
            await self._transition_state(RealtimeSessionState.LISTENING, reason)
        await self._record_metric(
            "utterance_discarded",
            generation_id=self._current_generation_id,
            stage="endpointing",
            reason_code=reason,
            metrics={
                "utterance_ms": audio_ms,
                "voiced_ms": self._turn_gate.last_voiced_ms,
            },
        )
        logger.info(
            "Realtime utterance discarded: session_id=%s reason=%s audio_ms=%s voiced_ms=%s",
            self.session_id,
            reason,
            audio_ms,
            self._turn_gate.last_voiced_ms,
            extra={
                "event": "realtime.turn_commit.discarded",
                "chat_id": str(self.chat_id),
                "character_id": str(self.character_id),
            },
        )

    async def _begin_soft_interrupt(self, reason: str) -> None:
        if self._soft_interrupt_active:
            return
        generation_id = self._current_generation_id
        if generation_id is None:
            return
        self._soft_interrupt_active = True
        await self.audio_pacer.pause(generation_id)
        await self._record_metric(
            "barge_in_detected",
            generation_id=generation_id,
            stage="interruption",
            reason_code=reason,
            metrics={"offset_ms": self._session_offset_ms()},
        )
        self._soft_interrupt_task = self._spawn(
            self._confirm_interrupt_after(reason, generation_id),
            name=f"realtime.interrupt.confirm.{self.session_id}.{generation_id}",
        )
        logger.info(
            "Realtime soft interrupt started: session_id=%s generation_id=%s reason=%s",
            self.session_id,
            generation_id,
            reason,
            extra={"event": "realtime.interrupt.soft_started"},
        )

    async def _confirm_interrupt_after(self, reason: str, generation_id: int) -> None:
        try:
            await asyncio.sleep(max(0, self.config.interrupt_hard_confirm_ms) / 1000)
            if (
                self._closed
                or not self._soft_interrupt_active
                or not self._owns_generation(generation_id)
            ):
                return
            # Backchannel tolerance: a short acknowledgement ("mm-hmm", "对")
            # should not kill a reply. Keep the audio paused and wait until the
            # user either produces enough voiced audio to count as a real
            # interruption, stops talking (the stop path resumes playback), or
            # the extra grace window expires.
            max_voiced_ms = max(0, self.config.backchannel_max_voiced_ms)
            resume_after_s = max(0, self.config.backchannel_max_wait_ms) / 1000
            watch_deadline = (
                time.monotonic() + max(0, self.config.backchannel_max_watch_ms) / 1000
            )
            started = time.monotonic()
            resumed = False
            while max_voiced_ms > 0:
                await asyncio.sleep(0.05)
                if (
                    self._closed
                    or not self._soft_interrupt_active
                    or not self._owns_generation(generation_id)
                ):
                    return
                if not self._speech_active:
                    # Speech stopped: the stop path resumes playback and
                    # discards this utterance.
                    return
                if max_voiced_ms > 0 and self._turn_gate.voiced_ms >= max_voiced_ms:
                    break
                if max_voiced_ms > 0 and time.monotonic() >= watch_deadline:
                    # Safety valve: never let backchannel watching hold the
                    # interrupt decision indefinitely (e.g. a stuck VAD).
                    break
                if not resumed and (time.monotonic() - started) >= resume_after_s:
                    # Below the interruption threshold for long enough: treat it
                    # as a backchannel and immediately resume the paused reply.
                    # Keep watching so a longer utterance can still interrupt,
                    # and keep the soft-interrupt flag set so the eventual
                    # speech stop discards this utterance instead of committing.
                    resumed = True
                    await self.audio_pacer.resume(generation_id)
                    await self._transition_state(
                        RealtimeSessionState.AGENT_SPEAKING,
                        "backchannel_resumed",
                    )
                    logger.info(
                        "Realtime soft interrupt treated as backchannel: session_id=%s "
                        "generation_id=%s voiced_ms=%s",
                        self.session_id,
                        generation_id,
                        self._turn_gate.voiced_ms,
                        extra={"event": "realtime.interrupt.backchannel_resumed"},
                    )
            self._soft_interrupt_active = False
            interrupted = await self.interrupt_manager.maybe_interrupt(
                reason=reason,
                generation_id=generation_id,
            )
            if interrupted:
                await self.cancel_response(reason, clear_audio=True)
                logger.info(
                    "Realtime hard interrupt confirmed: session_id=%s generation_id=%s reason=%s",
                    self.session_id,
                    generation_id,
                    reason,
                    extra={"event": "realtime.interrupt.confirmed"},
                )
                return
            await self.audio_pacer.resume(generation_id)
            await self._transition_state(
                RealtimeSessionState.AGENT_SPEAKING,
                "interrupt_not_confirmed",
            )
        except asyncio.CancelledError:
            raise
        finally:
            if self._soft_interrupt_task is asyncio.current_task():
                self._soft_interrupt_task = None

    async def _cancel_soft_interrupt(self, *, resume: bool, reason: str) -> None:
        if not resume:
            await self._cancel_pending_resume()
        current_task = asyncio.current_task()
        task = self._soft_interrupt_task
        if task and not task.done() and task is not current_task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        was_active = self._soft_interrupt_active
        if task is not current_task:
            self._soft_interrupt_task = None
        self._soft_interrupt_active = False
        generation_id = self._current_generation_id
        if resume and was_active and not self._closed and generation_id is not None:
            await self._cancel_pending_resume()
            self._resume_token += 1
            resume_token = self._resume_token
            self._resume_task = self._spawn(
                self._resume_after_grace(generation_id, resume_token, reason),
                name=f"realtime.interrupt.resume.{self.session_id}.{generation_id}",
            )

    async def _resume_after_grace(
        self,
        generation_id: int,
        resume_token: int,
        reason: str,
    ) -> None:
        try:
            await asyncio.sleep(max(0, self.config.interrupt_resume_grace_ms) / 1000)
            if (
                self._closed
                or self._speech_active
                or resume_token != self._resume_token
                or not self._owns_generation(generation_id)
            ):
                return
            if await self.audio_pacer.resume(generation_id):
                await self._transition_state(
                    RealtimeSessionState.AGENT_SPEAKING,
                    "soft_interrupt_resumed",
                )
                logger.info(
                    "Realtime soft interrupt resumed response: session_id=%s generation_id=%s reason=%s",
                    self.session_id,
                    generation_id,
                    reason,
                    extra={"event": "realtime.interrupt.resumed"},
                )
        finally:
            if self._resume_task is asyncio.current_task():
                self._resume_task = None

    async def _cancel_pending_resume(self) -> None:
        self._resume_token += 1
        task = self._resume_task
        self._resume_task = None
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run_response_after_start(
        self,
        start_event: asyncio.Event,
        utterance_pcm16_16k: bytes,
        generation_id: int,
        cancel_event: asyncio.Event,
    ) -> None:
        started = False
        try:
            await start_event.wait()
            started = True
            await self._handle_user_utterance(
                utterance_pcm16_16k,
                generation_id,
                cancel_event,
            )
        finally:
            if not started:
                self._cancel_reasons.pop(generation_id, None)
                if self._current_cancel_event is cancel_event:
                    self._current_cancel_event = None

    async def _handle_user_utterance(
        self,
        utterance_pcm16_16k: bytes,
        generation_id: int,
        cancel_event: asyncio.Event,
    ) -> None:
        turn_ctx: CurrentTurnContext | None = None
        tts_task: asyncio.Task[None] | None = None
        segment_queue: asyncio.Queue[str | None] | None = None
        stage = "stt"
        started_at = time.monotonic()
        outcome = "completed"
        failure_error_code: str | None = None

        try:
            self._ensure_generation(generation_id, cancel_event)
            await self._record_metric(
                "stt_start",
                generation_id=generation_id,
                stage="stt",
                metrics={"offset_ms": self._session_offset_ms()},
            )
            stt_started_at = time.monotonic()
            stt_stream = self._stt_stream
            self._stt_stream = None
            streaming_used = False
            raw_user_text = ""
            if stt_stream is not None:
                try:
                    self._ensure_generation(generation_id, cancel_event)
                    raw_user_text = await stt_stream.finish()
                    streaming_used = True
                except asyncio.CancelledError:
                    await stt_stream.abort()
                    raise
                except Exception as exc:
                    logger.warning(
                        "Realtime streaming STT failed, falling back to one-shot: "
                        "session_id=%s error_type=%s",
                        self.session_id,
                        type(exc).__name__,
                    )
                    await stt_stream.abort()
            if not raw_user_text:
                raw_user_text = await self.voice_runtime.transcribe_pcm16_mono(
                    utterance_pcm16_16k,
                    sample_rate=16000,
                )
            self._ensure_generation(generation_id, cancel_event)
            raw_user_text = (raw_user_text or "").strip()
            await self._record_metric(
                "stt_final",
                generation_id=generation_id,
                stage="stt",
                metrics={
                    "latency_ms": int((time.monotonic() - stt_started_at) * 1000),
                    "recognized_char_count": len(raw_user_text),
                    "is_empty": not bool(raw_user_text),
                    "stt_streaming": streaming_used,
                },
            )
            self._spawn(self._warm_stt_stream(), name=f"realtime.stt_warm.{self.session_id}")
            if not raw_user_text:
                await self._transition_state(RealtimeSessionState.LISTENING, "stt_empty")
                return

            await self.subtitle_bridge.publish_final(
                channel="user_raw",
                text=raw_user_text,
                generation_id=generation_id,
            )
            self._ensure_generation(generation_id, cancel_event)

            stage = "persistence"
            turn_ctx = await self.turn_service.begin_turn(
                user_id=self.user_id,
                chat_id=self.chat_id,
                character_id=self.character_id,
                raw_user_text=raw_user_text,
            )
            self._ensure_generation(generation_id, cancel_event)
            turn_ctx.generation_id = generation_id
            self._current_turn_ctx = turn_ctx
            await self.bus.publish(
                RealtimeEvent(
                    "conversation_turn_created",
                    {
                        "chat_id": str(self.chat_id),
                        "user_turn_id": str(turn_ctx.user_turn_id),
                        "user_candidate_id": str(turn_ctx.user_candidate_id),
                        "assistant_turn_id": str(turn_ctx.assistant_turn_id),
                        "assistant_candidate_id": str(turn_ctx.assistant_candidate_id),
                        "generation_id": generation_id,
                    },
                )
            )

            stage = "tts"
            if not await self.audio_pacer.begin_response(generation_id):
                raise asyncio.CancelledError
            await self.bus.publish(
                RealtimeEvent("response_started", {"generation_id": generation_id})
            )
            self._spawn(
                self._observe_pacer_generation(generation_id),
                name=f"realtime.pacer_observer.{self.session_id}.{generation_id}",
                generation_id=generation_id,
            )

            segment_queue = asyncio.Queue()
            await self._record_metric(
                "tts_start",
                generation_id=generation_id,
                stage="tts",
                metrics={"offset_ms": self._session_offset_ms()},
            )
            tts_task = self._spawn(
                self.voice_runtime.stream_tts_to_pacer(
                    segments_queue=segment_queue,
                    audio_pacer=self.audio_pacer,
                    character_id=self.character_id,
                    user_id=self.user_id,
                    debug_capture=None,
                    generation_id=generation_id,
                ),
                name=f"realtime.tts.{self.session_id}.{generation_id}",
                generation_id=generation_id,
            )

            tts_segmenter = self.voice_runtime.build_sentence_segmenter()
            assistant_partial_persisted = ""
            first_llm_chunk = True
            first_speakable = True
            tts_request_started = False

            async def enqueue_tts_segments(segments: list[str]) -> None:
                nonlocal assistant_partial_persisted, first_speakable, tts_request_started
                if not segments:
                    return
                self._ensure_generation(generation_id, cancel_event)
                if first_speakable:
                    first_speakable = False
                    await self._record_metric(
                        "first_speakable_chunk",
                        generation_id=generation_id,
                        stage="llm",
                        metrics={
                            "offset_ms": self._session_offset_ms(),
                            "speakable_char_count": len(segments[0]),
                        },
                    )
                for segment in segments:
                    self._ensure_generation(generation_id, cancel_event)
                    if not tts_request_started:
                        tts_request_started = True
                        await self._record_metric(
                            "tts_request_start",
                            generation_id=generation_id,
                            stage="tts",
                            metrics={"offset_ms": self._session_offset_ms()},
                        )
                    await segment_queue.put(segment)
                assistant_partial_persisted += "".join(segments)
                self._ensure_generation(generation_id, cancel_event)
                persisted = await self.turn_service.persist_assistant_partial(
                    assistant_turn_id=turn_ctx.assistant_turn_id,
                    assistant_candidate_id=turn_ctx.assistant_candidate_id,
                    content=assistant_partial_persisted,
                    user_id=self.user_id,
                )
                self._ensure_generation(generation_id, cancel_event)
                if persisted:
                    await self.bus.publish(
                        RealtimeEvent(
                            "conversation_turn_updated",
                            {
                                "chat_id": str(self.chat_id),
                                "assistant_turn_id": str(turn_ctx.assistant_turn_id),
                                "assistant_candidate_id": str(turn_ctx.assistant_candidate_id),
                                "is_final": False,
                                "generation_id": generation_id,
                            },
                        )
                    )

            stage = "llm"
            await self._record_metric(
                "llm_request",
                generation_id=generation_id,
                stage="llm",
                metrics={"offset_ms": self._session_offset_ms()},
            )
            llm_started_at = time.monotonic()
            async for chunk in self.dialog_service.stream_assistant_reply(
                user_id=self.user_id,
                chat_id=self.chat_id,
                character_id=self.character_id,
                history_leaf_turn_id=turn_ctx.history_leaf_turn_id,
                outbound_user_content=raw_user_text,
            ):
                self._ensure_generation(generation_id, cancel_event)
                if first_llm_chunk:
                    first_llm_chunk = False
                    await self._record_metric(
                        "llm_first_token",
                        generation_id=generation_id,
                        stage="llm",
                        metrics={
                            "latency_ms": int((time.monotonic() - llm_started_at) * 1000),
                            "offset_ms": self._session_offset_ms(),
                        },
                    )
                turn_ctx.assistant_accumulated_text += chunk
                await self.subtitle_bridge.publish_delta(
                    channel="assistant_raw",
                    text=chunk,
                    generation_id=generation_id,
                )
                ready_segments = tts_segmenter.feed(chunk)
                await enqueue_tts_segments(ready_segments)
                latency_segment = tts_segmenter.flush_for_latency()
                if latency_segment:
                    await enqueue_tts_segments([latency_segment])

            self._ensure_generation(generation_id, cancel_event)
            turn_ctx.llm_completed = True
            await enqueue_tts_segments(tts_segmenter.flush())
            await segment_queue.put(None)

            if self.config.translation_enabled and turn_ctx.assistant_accumulated_text.strip():
                self._spawn(
                    self._translate_and_publish(
                        generation_id,
                        cancel_event,
                        turn_ctx.assistant_accumulated_text,
                    ),
                    name=f"realtime.translation.{self.session_id}.{generation_id}",
                    generation_id=generation_id,
                )

            stage = "tts"
            await tts_task
            self._ensure_generation(generation_id, cancel_event)
            if not await self.audio_pacer.finish_response(generation_id):
                raise asyncio.CancelledError
            await self._record_metric(
                "tts_input_closed",
                generation_id=generation_id,
                stage="tts",
                metrics={"offset_ms": self._session_offset_ms()},
            )

            await self.subtitle_bridge.publish_final(
                channel="assistant_raw",
                text=turn_ctx.assistant_accumulated_text,
                generation_id=generation_id,
            )
            self._ensure_generation(generation_id, cancel_event)
            stage = "persistence"
            await self.turn_service.finalize_assistant_success(turn_ctx, user_id=self.user_id)
            self._ensure_generation(generation_id, cancel_event)
            await self.bus.publish(
                RealtimeEvent(
                    "conversation_turn_updated",
                    {
                        "chat_id": str(self.chat_id),
                        "assistant_turn_id": str(turn_ctx.assistant_turn_id),
                        "assistant_candidate_id": str(turn_ctx.assistant_candidate_id),
                        "is_final": True,
                        "generation_id": generation_id,
                    },
                )
            )

            stage = "playout"
            if not await self.audio_pacer.wait_until_drained(generation_id):
                raise asyncio.CancelledError
            self._ensure_generation(generation_id, cancel_event)
            stats = await self.audio_pacer.stats_snapshot()
            await self._record_metric(
                "server_playout_drained",
                generation_id=generation_id,
                stage="server_rtp_source",
                metrics={
                    "offset_ms": self._session_offset_ms(),
                    "drained_ms": stats.drained_ms or 0,
                    "peak_buffered_ms": stats.peak_buffered_ms,
                },
            )
            await self.bus.publish(
                RealtimeEvent(
                    "response_done",
                    {
                        "generation_id": generation_id,
                        "server_rtp_source_drained": True,
                    },
                )
            )
            await self._transition_state(RealtimeSessionState.LISTENING, "playout_drained")
        except asyncio.CancelledError:
            cancel_reason = self._cancel_reasons.get(generation_id, "cancelled")
            outcome = (
                "interrupted" if self._is_barge_in_reason(cancel_reason) else "cancelled"
            )
            if segment_queue is not None:
                with suppress(asyncio.QueueFull):
                    segment_queue.put_nowait(None)
            await self._cancel_task(tts_task)
            if turn_ctx is not None and self._owns_generation(generation_id):
                interrupted_is_final = await self.turn_service.finalize_assistant_interrupted(
                    turn_ctx,
                    user_id=self.user_id,
                )
                if self._owns_generation(generation_id):
                    await self.bus.publish(
                        RealtimeEvent(
                            "conversation_turn_updated",
                            {
                                "chat_id": str(self.chat_id),
                                "assistant_turn_id": str(turn_ctx.assistant_turn_id),
                                "assistant_candidate_id": str(turn_ctx.assistant_candidate_id),
                                "is_final": interrupted_is_final,
                                "generation_id": generation_id,
                            },
                        )
                    )
            if (
                self._owns_generation(generation_id)
                and self.state == RealtimeSessionState.THINKING
                and not self._speech_active
            ):
                await self._transition_state(
                    RealtimeSessionState.LISTENING,
                    "response_cancelled_before_playout",
                )
            raise
        except Exception as exc:
            outcome = "failed"
            logger.error(
                "Realtime response failed: session_id=%s generation_id=%s stage=%s error_type=%s",
                self.session_id,
                generation_id,
                stage,
                type(exc).__name__,
            )
            if segment_queue is not None:
                with suppress(asyncio.QueueFull):
                    segment_queue.put_nowait(None)
            await self._cancel_task(tts_task)
            fade_applied = await self.audio_pacer.clear_with_fadeout(generation_id)
            if fade_applied:
                await self._transition_state(
                    RealtimeSessionState.INTERRUPTING,
                    "response_failed",
                )
                self._spawn(
                    self._wait_for_audio_stopped(generation_id, f"{stage}_failure"),
                    name=f"realtime.audio_stop.{self.session_id}.{generation_id}",
                )
            error_code = self._error_code_for_stage(stage)
            failure_error_code = error_code
            safe_error_message = self._error_message_for_code(error_code)
            if turn_ctx is not None and self._owns_generation(generation_id):
                await self.turn_service.finalize_assistant_error(
                    turn_ctx,
                    safe_error_message,
                    user_id=self.user_id,
                )
                if self._owns_generation(generation_id):
                    await self.bus.publish(
                        RealtimeEvent(
                            "conversation_turn_failed",
                            {
                                "chat_id": str(self.chat_id),
                                "assistant_turn_id": str(turn_ctx.assistant_turn_id),
                                "assistant_candidate_id": str(turn_ctx.assistant_candidate_id),
                                "code": error_code,
                                "message": safe_error_message,
                                "generation_id": generation_id,
                            },
                        )
                    )
            if self._owns_generation(generation_id):
                await self.bus.publish(
                    RealtimeEvent(
                        "error",
                        {
                            "code": error_code,
                            "message": safe_error_message,
                            "generation_id": generation_id,
                        },
                    )
                )
            await self._record_metric(
                "failure",
                generation_id=generation_id,
                stage=stage,
                reason_code=error_code,
                metrics={"offset_ms": self._session_offset_ms()},
            )
            if self._owns_generation(generation_id) and not fade_applied:
                await self._transition_state(RealtimeSessionState.LISTENING, "response_failed")
        finally:
            stats = await self.audio_pacer.stats_snapshot()
            await self._upsert_turn_metrics(
                generation_id,
                turn_ctx,
                stats,
                outcome,
                failure_stage=stage if outcome == "failed" else None,
                error_code=failure_error_code,
            )
            logger.info(
                "Realtime response completed: session_id=%s generation_id=%s outcome=%s "
                "duration_ms=%s peak_buffered_ms=%s underrun_count=%s "
                "silence_frames=%s frames_read=%s",
                self.session_id,
                generation_id,
                outcome,
                int((time.monotonic() - started_at) * 1000),
                stats.peak_buffered_ms,
                stats.underrun_count,
                stats.silence_frames,
                stats.frames_read,
                extra={"event": "realtime.response.completed"},
            )
            if self._current_turn_ctx is turn_ctx:
                self._current_turn_ctx = None
            if self._current_cancel_event is cancel_event:
                self._current_cancel_event = None
            self._cancel_reasons.pop(generation_id, None)

    async def _translate_and_publish(
        self,
        generation_id: int,
        cancel_event: asyncio.Event,
        assistant_text: str,
    ) -> None:
        try:
            translated = await self.dialog_service.translate_assistant_chunk(assistant_text)
            self._ensure_generation(generation_id, cancel_event)
            if translated:
                await self.subtitle_bridge.publish_final(
                    channel="assistant_translated",
                    text=translated,
                    generation_id=generation_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Realtime assistant translation skipped: session_id=%s generation_id=%s "
                "error_type=%s",
                self.session_id,
                generation_id,
                type(exc).__name__,
            )

    async def _observe_pacer_generation(self, generation_id: int) -> None:
        if await self.audio_pacer.wait_until_first_audio_enqueued(generation_id):
            stats = await self.audio_pacer.stats_snapshot()
            if self._owns_generation(generation_id) and self.state == RealtimeSessionState.THINKING:
                await self._transition_state(
                    RealtimeSessionState.AGENT_SPEAKING,
                    "first_audio_enqueued",
                )
            await self._record_metric(
                "tts_first_audio",
                generation_id=generation_id,
                stage="tts",
                metrics={
                    "pacer_relative_ms": stats.first_audio_enqueued_ms or 0,
                    "offset_ms": self._session_offset_ms(),
                },
            )
            await self._record_metric(
                "first_audio_enqueued",
                generation_id=generation_id,
                stage="server_playout_queue",
                metrics={
                    "pacer_relative_ms": stats.first_audio_enqueued_ms or 0,
                    "buffered_ms": stats.buffered_ms,
                },
            )
        if await self.audio_pacer.wait_until_first_audio_read(generation_id):
            stats = await self.audio_pacer.stats_snapshot()
            await self._record_metric(
                "server_first_audio_read",
                generation_id=generation_id,
                stage="server_rtp_source",
                metrics={
                    "pacer_relative_ms": stats.first_audio_read_ms or 0,
                    "is_browser_playback": False,
                },
            )

    async def _wait_for_audio_stopped(self, generation_id: int, reason: str) -> None:
        drained = await self.audio_pacer.wait_until_drained(generation_id)
        await self._record_metric(
            "audio_stopped",
            generation_id=generation_id,
            stage="interruption",
            reason_code=reason,
            metrics={
                "offset_ms": self._session_offset_ms(),
                "fade_consumed": drained,
            },
        )
        await self.bus.publish(
            RealtimeEvent(
                "response_audio_stopped",
                {
                    "generation_id": generation_id,
                    "reason": reason,
                    "server_rtp_source_stopped": True,
                },
            )
        )
        if self._owns_generation(generation_id) and self.state == RealtimeSessionState.INTERRUPTING:
            if self._speech_active:
                target_state = RealtimeSessionState.USER_SPEAKING
            elif self._turn_gate.has_pending_commit:
                target_state = RealtimeSessionState.ENDPOINT_PENDING
            else:
                target_state = RealtimeSessionState.LISTENING
            await self._transition_state(target_state, "audio_stopped")

    async def _transition_state(self, state: RealtimeSessionState, reason: str) -> None:
        state = RealtimeSessionState(state)
        previous = self._state
        if previous == state:
            return
        self._state = state
        offset_ms = self._session_offset_ms()
        logger.info(
            "Realtime state transitioned: session_id=%s generation_id=%s from_state=%s to_state=%s reason=%s offset_ms=%s",
            self.session_id,
            self._current_generation_id,
            previous.value,
            state.value,
            reason,
            offset_ms,
            extra={"event": "realtime.state.transition"},
        )
        await self._record_metric(
            "state_transition",
            generation_id=self._current_generation_id,
            stage="state_machine",
            reason_code=reason,
            metrics={
                "previous_state": previous.value,
                "state": state.value,
                "offset_ms": offset_ms,
            },
        )

    def _ensure_generation(self, generation_id: int, cancel_event: asyncio.Event) -> None:
        if self._closed or cancel_event.is_set() or not self._owns_generation(generation_id):
            raise asyncio.CancelledError

    def _owns_generation(self, generation_id: int) -> bool:
        return generation_id == self._current_generation_id

    @staticmethod
    def _is_barge_in_reason(reason: str) -> bool:
        return reason in {
            "user_speech_detected",
            "user_speech_while_thinking",
            "user_utterance_committed",
        }

    def _session_offset_ms(self) -> int:
        return max(0, int((time.monotonic() - self._started_monotonic) * 1000))

    def _spawn(
        self,
        coroutine: Coroutine[Any, Any, _TaskResult],
        *,
        name: str,
        generation_id: int | None = None,
    ) -> asyncio.Task[_TaskResult]:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        if generation_id is not None:
            self._generation_tasks.setdefault(generation_id, set()).add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            if generation_id is not None:
                generation_tasks = self._generation_tasks.get(generation_id)
                if generation_tasks is not None:
                    generation_tasks.discard(completed)
                    if not generation_tasks:
                        self._generation_tasks.pop(generation_id, None)
            if completed.cancelled():
                return
            exception = completed.exception()
            if exception is not None:
                logger.error(
                    "Realtime tracked task failed: session_id=%s generation_id=%s task=%s "
                    "error_type=%s",
                    self.session_id,
                    generation_id,
                    completed.get_name(),
                    type(exception).__name__,
                )

        task.add_done_callback(_done)
        return task

    def _clear_response_task(self, completed: asyncio.Task[None]) -> None:
        if self._response_task is completed:
            self._response_task = None

    @staticmethod
    async def _cancel_task(task: asyncio.Task[Any] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _record_metric(
        self,
        event_type: str,
        *,
        generation_id: int | None = None,
        stage: str | None = None,
        reason_code: str | None = None,
        metrics: MetricPayload | None = None,
    ) -> None:
        if self._metrics_sink is None:
            return
        try:
            event_type, stage, reason_code = sanitize_metric_context(
                event_type=event_type,
                stage=stage,
                reason_code=reason_code,
            )
            sanitized = sanitize_metric_payload(metrics)
            await self._metrics_sink.record_event(
                session_id=self.session_id,
                event_type=event_type,
                generation_id=generation_id,
                stage=stage,
                reason_code=reason_code,
                metrics=sanitized,
            )
        except Exception as exc:
            logger.warning(
                "Realtime metric event rejected or unavailable: session_id=%s event_type=%s "
                "error_type=%s",
                self.session_id,
                event_type,
                type(exc).__name__,
            )

    async def _upsert_turn_metrics(
        self,
        generation_id: int,
        turn_ctx: CurrentTurnContext | None,
        stats: AudioPacerStats,
        outcome: str,
        *,
        failure_stage: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._metrics_sink is None:
            return
        metrics: dict[str, int | float | bool | str] = {
            "outcome": outcome,
            "buffered_ms": stats.buffered_ms,
            "peak_buffered_ms": stats.peak_buffered_ms,
            "underrun_count": stats.underrun_count,
            "partial_pad_count": stats.partial_pad_count,
            "frames_read": stats.frames_read,
            "silence_frames": stats.silence_frames,
            "rejected_write_count": stats.rejected_write_count,
            "fadeout_count": stats.fadeout_count,
        }
        if failure_stage is not None:
            metrics["failure_stage"] = failure_stage
        if error_code is not None:
            metrics["error_code"] = error_code
        if stats.first_audio_enqueued_ms is not None:
            metrics["first_audio_enqueued_ms"] = stats.first_audio_enqueued_ms
        if stats.first_audio_read_ms is not None:
            metrics["server_first_audio_read_ms"] = stats.first_audio_read_ms
        if stats.drained_ms is not None:
            metrics["server_rtp_source_drained_ms"] = stats.drained_ms
        try:
            await self._metrics_sink.upsert_turn_metrics(
                session_id=self.session_id,
                generation_id=generation_id,
                turn_id=turn_ctx.assistant_turn_id if turn_ctx is not None else None,
                metrics=sanitize_metric_payload(metrics),
            )
        except Exception as exc:
            logger.warning(
                "Realtime turn metrics unavailable: session_id=%s generation_id=%s error_type=%s",
                self.session_id,
                generation_id,
                type(exc).__name__,
            )

    @staticmethod
    def _error_code_for_stage(stage: str) -> str:
        if stage == "stt":
            return "stt_upstream_error"
        if stage == "tts":
            return "tts_upstream_error"
        if stage == "llm":
            return "llm_service_error"
        return "internal_error"

    @staticmethod
    def _error_message_for_code(error_code: str) -> str:
        return {
            "stt_upstream_error": "语音识别暂时不可用，请重试。",
            "tts_upstream_error": "语音合成暂时不可用，请重试。",
            "llm_service_error": "回复生成暂时不可用，请重试。",
        }.get(error_code, "实时通话暂时不可用，请重试。")
