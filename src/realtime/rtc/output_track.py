"""Outbound bot audio track for aiortc."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from fractions import Fraction

import av
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

from src.realtime.pipeline.audio_pacer import AudioPacer


class BotAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(
        self,
        pacer: AudioPacer,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__()
        self._pacer = pacer
        self._timestamp = 0
        self._start: float | None = None
        self._monotonic = monotonic
        self._sleep = sleep

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        if self._start is None:
            self._start = self._monotonic()
        else:
            target_time = self._start + (self._timestamp / self._pacer.sample_rate)
            delay = target_time - self._monotonic()
            if delay > 0:
                await self._sleep(delay)

        raw = await self._pacer.read_frame()
        raw = self._normalize_frame_bytes(raw)
        frame = av.AudioFrame(format="s16", layout="mono", samples=self._pacer.frame_samples)
        frame.sample_rate = self._pacer.sample_rate
        frame.planes[0].update(raw)
        frame.pts = self._timestamp
        frame.time_base = Fraction(1, self._pacer.sample_rate)
        self._timestamp += self._pacer.frame_samples
        return frame

    def _normalize_frame_bytes(self, raw: bytes) -> bytes:
        expected_bytes = self._pacer.frame_samples * 2
        if len(raw) == expected_bytes:
            return raw
        if len(raw) > expected_bytes:
            return raw[:expected_bytes]
        return raw + bytes(expected_bytes - len(raw))
