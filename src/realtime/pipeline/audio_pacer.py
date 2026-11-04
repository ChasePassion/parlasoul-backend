"""Generation-aware PCM pacing and interrupt-safe output buffering."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class AudioPacerStats:
    generation_id: int = 0
    buffered_ms: int = 0
    peak_buffered_ms: int = 0
    underrun_count: int = 0
    partial_pad_count: int = 0
    frames_read: int = 0
    silence_frames: int = 0
    rejected_write_count: int = 0
    fadeout_count: int = 0
    first_audio_enqueued_ms: int | None = None
    # This is when aiortc pulls the first source frame. It is not proof that
    # the browser has played the frame and must not be labelled first_played.
    first_audio_read_ms: int | None = None
    drained_ms: int | None = None


@dataclass(slots=True)
class _GenerationSignals:
    first_enqueued: asyncio.Event
    first_read: asyncio.Event
    drained: asyncio.Event
    valid: bool = True
    was_drained: bool = False


class AudioPacer:
    """Maintain fixed-duration PCM frames and fence stale response writers."""

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        channels: int = 1,
        frame_ms: int = 20,
        prebuffer_ms: int = 200,
        recovery_prebuffer_ms: int = 60,
        fadeout_ms: int = 50,
        max_buffer_ms: int = 2000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.prebuffer_ms = prebuffer_ms
        self.recovery_prebuffer_ms = recovery_prebuffer_ms
        self.fadeout_ms = fadeout_ms
        self.max_buffer_ms = max_buffer_ms
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self._max_buffer_samples = max(1, int(sample_rate * max_buffer_ms / 1000))
        self._monotonic = monotonic

        self._buffer = np.zeros(0, dtype=np.int16)
        self._fade_buffer = np.zeros(0, dtype=np.int16)
        self._accumulating = False
        self._recovering = False
        self._input_closed = False
        self._response_active = True
        self._paused = False
        self._closed = False

        # Generation zero preserves the old standalone-pacer behavior for
        # tests and non-session callers which write before begin_response().
        self._current_generation_id = 0
        self._generation_counter = 0
        self._begin_monotonic = self._monotonic()
        self._fadeout_applied_generation: int | None = None
        self._stats = AudioPacerStats(generation_id=0)
        self._signals: dict[int, _GenerationSignals] = {0: self._new_signals()}

        self._lock = asyncio.Lock()
        self._data_event = asyncio.Event()
        self._space_event = asyncio.Event()
        self._space_event.set()

    @property
    def current_generation_id(self) -> int:
        return self._current_generation_id

    async def begin_response(self, generation_id: int | None = None) -> bool:
        """Open a newer generation and invalidate every prior writer/waiter."""

        async with self._lock:
            if self._closed:
                return False
            resolved = self._generation_counter + 1 if generation_id is None else generation_id
            if resolved <= self._current_generation_id:
                return False

            previous = self._signals.get(self._current_generation_id)
            if previous is not None and not previous.was_drained:
                self._invalidate_signals_locked(previous)

            self._generation_counter = max(self._generation_counter, resolved)
            self._current_generation_id = resolved
            self._begin_monotonic = self._monotonic()
            self._fadeout_applied_generation = None
            self._buffer = np.zeros(0, dtype=np.int16)
            self._fade_buffer = np.zeros(0, dtype=np.int16)
            self._accumulating = True
            self._recovering = False
            self._input_closed = False
            self._response_active = True
            self._paused = False
            self._stats = AudioPacerStats(generation_id=resolved)
            self._signals[resolved] = self._new_signals()
            self._prune_signals_locked()
            self._data_event.clear()
            self._update_space_event_locked()
            return True

    async def write_pcm16(self, data: bytes, *, generation_id: int | None = None) -> bool:
        """Write PCM for the current generation, rejecting stale producers."""

        if not data:
            return True
        samples = np.frombuffer(data, dtype=np.int16).copy()
        if samples.size == 0:
            return True

        resolved = self._resolve_generation(generation_id)
        offset = 0
        while offset < samples.size:
            async with self._lock:
                if not self._can_write_locked(resolved):
                    self._stats.rejected_write_count += 1
                    return False
                available_samples = self._max_buffer_samples - self._buffer.size
                if available_samples > 0:
                    take_samples = min(available_samples, samples.size - offset)
                    chunk = samples[offset : offset + take_samples]
                    self._buffer = np.concatenate((self._buffer, chunk))
                    offset += take_samples
                    if self._stats.first_audio_enqueued_ms is None:
                        self._stats.first_audio_enqueued_ms = self._elapsed_ms_locked()
                        self._signals[resolved].first_enqueued.set()
                    self._data_event.set()
                    self._update_buffered_ms_locked()
                    self._update_space_event_locked()
                    continue
                self._update_space_event_locked()
            await self._space_event.wait()
        return True

    async def finish_response(self, generation_id: int | None = None) -> bool:
        """Close producer input; drain completes only after recv consumes audio."""

        resolved = self._resolve_generation(generation_id)
        async with self._lock:
            if not self._is_current_locked(resolved):
                return False
            self._input_closed = True
            self._data_event.set()
            if self._buffer.size == 0 and self._fade_buffer.size == 0:
                self._mark_drained_locked(resolved)
            self._update_space_event_locked()
            return True

    async def wait_until_drained(self, generation_id: int | None = None) -> bool:
        resolved = self._resolve_generation(generation_id)
        async with self._lock:
            signals = self._signals.get(resolved)
            if signals is None:
                return False
            event = signals.drained
        await event.wait()
        return signals.valid and signals.was_drained

    async def wait_until_first_audio_enqueued(self, generation_id: int) -> bool:
        async with self._lock:
            signals = self._signals.get(generation_id)
            if signals is None:
                return False
            event = signals.first_enqueued
        await event.wait()
        return (
            signals.valid
            and self._stats.generation_id == generation_id
            and self._stats.first_audio_enqueued_ms is not None
        )

    async def wait_until_first_audio_read(self, generation_id: int) -> bool:
        async with self._lock:
            signals = self._signals.get(generation_id)
            if signals is None:
                return False
            event = signals.first_read
        await event.wait()
        return (
            signals.valid
            and self._stats.generation_id == generation_id
            and self._stats.first_audio_read_ms is not None
        )

    async def read_frame(self) -> bytes:
        if self._closed:
            return self._silence_bytes()

        async with self._lock:
            generation_id = self._current_generation_id
            if self._paused:
                return self._silence_frame_locked()

            if self._fade_buffer.size >= self.frame_samples:
                frame = self._fade_buffer[: self.frame_samples]
                self._fade_buffer = self._fade_buffer[self.frame_samples :]
                self._record_audio_read_locked(generation_id)
                self._update_buffered_ms_locked()
                if self._fade_buffer.size == 0 and self._input_closed:
                    self._mark_drained_locked(generation_id)
                return frame.astype(np.int16, copy=False).tobytes()

            if self._fade_buffer.size > 0:
                frame = self._fade_buffer
                self._fade_buffer = np.zeros(0, dtype=np.int16)
                self._record_audio_read_locked(generation_id)
                self._stats.partial_pad_count += 1
                self._update_buffered_ms_locked()
                if self._input_closed:
                    self._mark_drained_locked(generation_id)
                return self._pad_frame(frame).tobytes()

            if self._accumulating:
                target_samples = self.frame_samples * self._current_prebuffer_frames_locked()
                if self._input_closed and self._buffer.size > 0:
                    self._accumulating = False
                    self._recovering = False
                elif self._buffer.size < target_samples:
                    self._update_buffered_ms_locked()
                    if self._input_closed and self._buffer.size == 0:
                        self._mark_drained_locked(generation_id)
                    return self._silence_frame_locked()
                else:
                    self._accumulating = False
                    self._recovering = False

            if self._buffer.size >= self.frame_samples:
                frame = self._buffer[: self.frame_samples]
                self._buffer = self._buffer[self.frame_samples :]
                if self._buffer.size == 0:
                    self._data_event.clear()
                self._record_audio_read_locked(generation_id)
                self._update_buffered_ms_locked()
                self._update_space_event_locked()
                if self._buffer.size == 0 and self._input_closed:
                    self._mark_drained_locked(generation_id)
                return frame.astype(np.int16, copy=False).tobytes()

            if self._buffer.size > 0:
                if self._input_closed:
                    frame = self._buffer
                    self._buffer = np.zeros(0, dtype=np.int16)
                    self._data_event.clear()
                    self._record_audio_read_locked(generation_id)
                    self._stats.partial_pad_count += 1
                    self._update_buffered_ms_locked()
                    self._update_space_event_locked()
                    self._mark_drained_locked(generation_id)
                    return self._pad_frame(frame).tobytes()
                self._data_event.clear()
                self._enter_recovery_locked()
                self._update_space_event_locked()
                return self._silence_frame_locked()

            self._data_event.clear()
            self._update_space_event_locked()
            if self._input_closed:
                self._mark_drained_locked(generation_id)
            elif self._response_active:
                self._enter_recovery_locked()
                return self._silence_frame_locked()
            return self._silence_bytes()

    async def clear(self, generation_id: int | None = None) -> bool:
        """Reset the current generation while keeping its producer input open."""

        resolved = self._resolve_generation(generation_id)
        async with self._lock:
            if not self._is_current_locked(resolved):
                return False
            old_signals = self._signals[resolved]
            self._invalidate_signals_locked(old_signals)
            self._signals[resolved] = self._new_signals()
            self._buffer = np.zeros(0, dtype=np.int16)
            self._fade_buffer = np.zeros(0, dtype=np.int16)
            self._accumulating = True
            self._recovering = False
            self._input_closed = False
            self._response_active = True
            self._paused = False
            self._fadeout_applied_generation = None
            self._begin_monotonic = self._monotonic()
            self._stats = AudioPacerStats(generation_id=resolved)
            self._data_event.clear()
            self._update_space_event_locked()
            return True

    async def clear_with_fadeout(self, generation_id: int | None = None) -> bool:
        """Stop a generation once, preserving only a bounded fade tail."""

        resolved = self._resolve_generation(generation_id)
        async with self._lock:
            if not self._is_current_locked(resolved):
                return False
            if self._fadeout_applied_generation == resolved:
                return False

            self._fadeout_applied_generation = resolved
            self._stats.fadeout_count += 1
            fade_samples = int(self.sample_rate * self.fadeout_ms / 1000)
            if fade_samples > 0 and self._buffer.size > 0:
                fade = self._buffer[:fade_samples].copy()
                envelope = np.linspace(1.0, 0.0, num=fade.size, endpoint=True)
                self._fade_buffer = np.clip(
                    fade.astype(np.float32) * envelope,
                    -32768,
                    32767,
                ).astype(np.int16)
            else:
                self._fade_buffer = np.zeros(0, dtype=np.int16)

            self._buffer = np.zeros(0, dtype=np.int16)
            self._accumulating = False
            self._recovering = False
            self._input_closed = True
            self._response_active = self._fade_buffer.size > 0
            self._paused = False
            self._data_event.set()
            self._update_buffered_ms_locked()
            self._update_space_event_locked()
            if self._fade_buffer.size == 0:
                self._mark_drained_locked(resolved)
            return True

    async def pause(self, generation_id: int | None = None) -> bool:
        resolved = self._resolve_generation(generation_id)
        async with self._lock:
            if not self._is_current_locked(resolved):
                return False
            self._paused = True
            return True

    async def resume(self, generation_id: int | None = None) -> bool:
        resolved = self._resolve_generation(generation_id)
        async with self._lock:
            if not self._is_current_locked(resolved) or self._input_closed:
                return False
            self._paused = False
            if self._buffer.size > 0 or self._fade_buffer.size > 0:
                self._data_event.set()
            return True

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            signals = self._signals.get(self._current_generation_id)
            if signals is not None and not signals.was_drained:
                self._invalidate_signals_locked(signals)
            self._buffer = np.zeros(0, dtype=np.int16)
            self._fade_buffer = np.zeros(0, dtype=np.int16)
            self._recovering = False
            self._response_active = False
            self._data_event.set()
            self._update_space_event_locked()

    async def stats_snapshot(self) -> AudioPacerStats:
        async with self._lock:
            self._update_buffered_ms_locked()
            return AudioPacerStats(
                generation_id=self._stats.generation_id,
                buffered_ms=self._stats.buffered_ms,
                peak_buffered_ms=self._stats.peak_buffered_ms,
                underrun_count=self._stats.underrun_count,
                partial_pad_count=self._stats.partial_pad_count,
                frames_read=self._stats.frames_read,
                silence_frames=self._stats.silence_frames,
                rejected_write_count=self._stats.rejected_write_count,
                fadeout_count=self._stats.fadeout_count,
                first_audio_enqueued_ms=self._stats.first_audio_enqueued_ms,
                first_audio_read_ms=self._stats.first_audio_read_ms,
                drained_ms=self._stats.drained_ms,
            )

    def _resolve_generation(self, generation_id: int | None) -> int:
        return self._current_generation_id if generation_id is None else generation_id

    def _is_current_locked(self, generation_id: int) -> bool:
        return not self._closed and generation_id == self._current_generation_id

    def _can_write_locked(self, generation_id: int) -> bool:
        return self._is_current_locked(generation_id) and not self._input_closed

    def _record_audio_read_locked(self, generation_id: int) -> None:
        self._stats.frames_read += 1
        if self._stats.first_audio_read_ms is None:
            self._stats.first_audio_read_ms = self._elapsed_ms_locked()
            self._signals[generation_id].first_read.set()

    def _mark_drained_locked(self, generation_id: int) -> None:
        signals = self._signals.get(generation_id)
        if signals is None or signals.was_drained:
            return
        self._response_active = False
        self._stats.drained_ms = self._elapsed_ms_locked()
        signals.was_drained = True
        signals.first_enqueued.set()
        signals.first_read.set()
        signals.drained.set()

    def _silence_frame_locked(self) -> bytes:
        if self._response_active:
            self._stats.silence_frames += 1
        return self._silence_bytes()

    def _silence_bytes(self) -> bytes:
        return np.zeros(self.frame_samples, dtype=np.int16).tobytes()

    def _pad_frame(self, frame: np.ndarray) -> np.ndarray:
        if frame.size >= self.frame_samples:
            return frame[: self.frame_samples]
        padding = np.zeros(self.frame_samples - frame.size, dtype=np.int16)
        return np.concatenate((frame.astype(np.int16, copy=False), padding))

    def _elapsed_ms_locked(self) -> int:
        return max(0, int((self._monotonic() - self._begin_monotonic) * 1000))

    def _update_buffered_ms_locked(self) -> None:
        buffered_samples = self._buffer.size + self._fade_buffer.size
        self._stats.buffered_ms = int(buffered_samples / self.sample_rate * 1000)
        self._stats.peak_buffered_ms = max(
            self._stats.peak_buffered_ms,
            self._stats.buffered_ms,
        )

    def _update_space_event_locked(self) -> None:
        if self._closed or self._input_closed or self._buffer.size < self._max_buffer_samples:
            self._space_event.set()
        else:
            self._space_event.clear()

    def _current_prebuffer_frames_locked(self) -> int:
        prebuffer_ms = self.recovery_prebuffer_ms if self._recovering else self.prebuffer_ms
        return max(1, math.ceil(prebuffer_ms / self.frame_ms))

    def _enter_recovery_locked(self) -> None:
        if self._input_closed or not self._response_active:
            self._update_buffered_ms_locked()
            return
        if not self._accumulating:
            self._stats.underrun_count += 1
        self._accumulating = True
        self._recovering = True
        self._update_buffered_ms_locked()

    @staticmethod
    def _new_signals() -> _GenerationSignals:
        return _GenerationSignals(
            first_enqueued=asyncio.Event(),
            first_read=asyncio.Event(),
            drained=asyncio.Event(),
        )

    @staticmethod
    def _invalidate_signals_locked(signals: _GenerationSignals) -> None:
        signals.valid = False
        signals.first_enqueued.set()
        signals.first_read.set()
        signals.drained.set()

    def _prune_signals_locked(self) -> None:
        minimum_generation = self._current_generation_id - 8
        for generation_id in list(self._signals):
            if generation_id < minimum_generation:
                self._signals.pop(generation_id, None)
