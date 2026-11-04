"""Silero-backed full-duplex VAD and interrupt coordination."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from src.realtime.audio.ring_buffer import RingBuffer
from src.realtime.pipeline.audio_pacer import AudioPacer
from src.realtime.pipeline.bus import RealtimeEventBus
from src.realtime.pipeline.events import RealtimeEvent
from src.realtime.session.state import RealtimeSessionState
from src.realtime.vad.silero import SileroVADStream

logger = logging.getLogger(__name__)

WINDOW_SIZE = 512


@dataclass(slots=True)
class VADTransition:
    speech_started: bool = False
    speech_stopped: bool = False
    preroll_audio: bytes = b""
    confidence: float = 0.0
    audio_ms: int = 0
    voiced_ms_delta: int = 0


class InterruptManager:
    def __init__(
        self,
        *,
        bus: RealtimeEventBus,
        audio_pacer: AudioPacer,
        vad_stream: SileroVADStream,
        sample_rate: int = 16000,
        vad_threshold: float = 0.5,
        vad_end_threshold_delta: float = 0.15,
        silence_ms: int = 800,
        speech_pad_ms: int = 30,
        preroll_ms: int = 300,
        interrupt_cooldown_ms: int = 500,
    ) -> None:
        self._bus = bus
        self._audio_pacer = audio_pacer
        self._vad_stream = vad_stream
        self._sample_rate = sample_rate
        self._vad_threshold = vad_threshold
        self._end_threshold = max(0.0, vad_threshold - vad_end_threshold_delta)
        self._silence_ms = silence_ms
        self._speech_pad_ms = speech_pad_ms
        self._interrupt_cooldown_ms = interrupt_cooldown_ms

        self._state = RealtimeSessionState.LISTENING
        self._state_getter: Callable[[], RealtimeSessionState] | None = None
        self._state_transition: (
            Callable[[RealtimeSessionState, str], Awaitable[None]] | None
        ) = None
        self._sample_buffer = np.zeros(0, dtype=np.float32)
        self._preroll_buffer = RingBuffer(sample_rate, preroll_ms)
        self._triggered = False
        self._temp_end_sample = 0
        self._last_interrupt_at = 0.0

    @property
    def state(self) -> RealtimeSessionState:
        if self._state_getter is not None:
            return self._state_getter()
        return self._state

    @state.setter
    def state(self, value: RealtimeSessionState) -> None:
        # Compatibility for isolated unit tests. A bound session owns state.
        self._state = RealtimeSessionState(value)

    def bind_state_controller(
        self,
        *,
        getter: Callable[[], RealtimeSessionState],
        transition: Callable[[RealtimeSessionState, str], Awaitable[None]],
    ) -> None:
        self._state_getter = getter
        self._state_transition = transition

    async def ingest_frame(self, pcm16: bytes, *, frame_ms: int) -> VADTransition:
        transition = VADTransition()
        samples_i16 = np.frombuffer(pcm16, dtype=np.int16)
        if samples_i16.size == 0:
            return transition

        self._preroll_buffer.write(pcm16)
        samples = samples_i16.astype(np.float32) / 32768.0
        self._sample_buffer = np.concatenate((self._sample_buffer, samples))

        min_silence_samples = self._silence_ms * self._sample_rate // 1000
        speech_pad_samples = self._speech_pad_ms * self._sample_rate // 1000

        while self._sample_buffer.size >= WINDOW_SIZE:
            window = self._sample_buffer[:WINDOW_SIZE]
            self._sample_buffer = self._sample_buffer[WINDOW_SIZE:]

            speech_prob = self._vad_stream.infer(window)
            current_sample = self._vad_stream.curr_sample

            if speech_prob >= self._vad_threshold:
                transition.voiced_ms_delta += WINDOW_SIZE * 1000 // self._sample_rate

            if speech_prob >= self._vad_threshold and self._temp_end_sample != 0:
                self._temp_end_sample = 0

            if speech_prob >= self._vad_threshold and not self._triggered:
                self._triggered = True
                speech_start_sample = max(0, current_sample - WINDOW_SIZE - speech_pad_samples)
                transition.speech_started = True
                transition.preroll_audio = self._preroll_buffer.read_all()
                transition.confidence = speech_prob
                transition.audio_ms = speech_start_sample * 1000 // self._sample_rate
                self._preroll_buffer.clear()

            if speech_prob < self._end_threshold and self._triggered:
                if self._temp_end_sample == 0:
                    self._temp_end_sample = current_sample

                if current_sample - self._temp_end_sample >= min_silence_samples:
                    speech_end_sample = self._temp_end_sample + speech_pad_samples
                    self._triggered = False
                    self._temp_end_sample = 0
                    transition.speech_stopped = True
                    transition.confidence = speech_prob
                    transition.audio_ms = speech_end_sample * 1000 // self._sample_rate

        if transition.speech_started:
            await self._transition_state(
                RealtimeSessionState.USER_SPEAKING,
                "vad_speech_started",
            )
            await self._bus.publish(
                RealtimeEvent(
                    "speech_started",
                    {
                        "at_ms": int(time.time() * 1000),
                        "audio_ms": transition.audio_ms,
                        "confidence": transition.confidence,
                    },
                )
            )

        if transition.speech_stopped:
            if self.state == RealtimeSessionState.USER_SPEAKING:
                await self._transition_state(
                    RealtimeSessionState.ENDPOINT_PENDING,
                    "vad_speech_stopped",
                )
            await self._bus.publish(
                RealtimeEvent(
                    "speech_stopped",
                    {
                        "at_ms": int(time.time() * 1000),
                        "audio_ms": transition.audio_ms,
                        "confidence": transition.confidence,
                    },
                )
            )

        return transition

    async def maybe_interrupt(self, *, reason: str, generation_id: int | None = None) -> bool:
        now_ms = time.monotonic() * 1000
        if self.state not in {
            RealtimeSessionState.AGENT_SPEAKING,
            RealtimeSessionState.USER_SPEAKING,
            RealtimeSessionState.ENDPOINT_PENDING,
        }:
            return False
        if now_ms - self._last_interrupt_at < self._interrupt_cooldown_ms:
            return False

        self._last_interrupt_at = now_ms
        await self._transition_state(RealtimeSessionState.INTERRUPTING, reason)
        payload: dict[str, object] = {"reason": reason, "at_ms": int(time.time() * 1000)}
        if generation_id is not None:
            payload["generation_id"] = generation_id
        await self._bus.publish(
            RealtimeEvent("response_interrupted", payload)
        )
        logger.info("Realtime response interrupted: reason=%s", reason)
        return True

    def mark_thinking(self) -> None:
        self._state = RealtimeSessionState.THINKING

    def mark_idle(self) -> None:
        self._state = RealtimeSessionState.LISTENING

    def mark_response_resumed(self) -> None:
        self._state = RealtimeSessionState.AGENT_SPEAKING

    async def mark_response_started(self) -> None:
        await self._transition_state(
            RealtimeSessionState.AGENT_SPEAKING,
            "response_started",
        )
        await self._bus.publish(RealtimeEvent("response_started", {}))

    async def mark_response_done(self) -> None:
        await self._transition_state(RealtimeSessionState.LISTENING, "response_done")
        await self._bus.publish(RealtimeEvent("response_done", {}))

    async def _transition_state(self, state: RealtimeSessionState, reason: str) -> None:
        if self._state_transition is not None:
            await self._state_transition(state, reason)
            return
        self._state = state
