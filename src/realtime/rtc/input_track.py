"""Inbound user audio track reader."""

from __future__ import annotations

import asyncio
import logging

import av
import numpy as np
from aiortc.mediastreams import MediaStreamError

logger = logging.getLogger(__name__)


def frame_to_mono_pcm16(frame) -> tuple[np.ndarray, int]:
    array = frame.to_ndarray()
    if array.ndim == 1:
        mono = array
    else:
        mono = array.mean(axis=0)

    if mono.dtype != np.int16:
        mono = mono.astype(np.int16)
    return mono, int(getattr(frame, "sample_rate", 48000) or 48000)


def resample_pcm16(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if samples.size == 0 or source_rate == target_rate:
        return samples.astype(np.int16, copy=False)

    duration = samples.size / source_rate
    target_size = max(1, int(round(duration * target_rate)))
    source_positions = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    target_positions = np.linspace(0.0, 1.0, num=target_size, endpoint=False)
    resampled = np.interp(target_positions, source_positions, samples.astype(np.float32))
    return np.clip(resampled, -32768, 32767).astype(np.int16)


class InputTrackReader:
    def __init__(self, *, track, on_pcm16_frame) -> None:
        self._track = track
        self._on_pcm16_frame = on_pcm16_frame
        self._task: asyncio.Task[None] | None = None
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="realtime.input_track")

    async def stop(self) -> None:
        if self._task:
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                frame = await self._track.recv()
                resampled_frames = self._resampler.resample(frame)
                if not isinstance(resampled_frames, list):
                    resampled_frames = [resampled_frames]
                for resampled in resampled_frames:
                    if resampled is None:
                        continue
                    mono, sample_rate = frame_to_mono_pcm16(resampled)
                    await self._on_pcm16_frame(mono.tobytes(), sample_rate)
        except MediaStreamError:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Realtime input track reader failed: error_type=%s",
                type(exc).__name__,
            )
