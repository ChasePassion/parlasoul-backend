from __future__ import annotations

import numpy as np
import pytest

from src.realtime.pipeline.audio_pacer import AudioPacer
from src.realtime.rtc.output_track import BotAudioTrack


@pytest.mark.asyncio
async def test_bot_audio_track_recv_uses_20ms_media_clock() -> None:
    now = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    pacer = AudioPacer(sample_rate=48000, frame_ms=20, prebuffer_ms=20)
    track = BotAudioTrack(pacer, monotonic=monotonic, sleep=sleep)
    for offset in range(3):
        samples = np.full(pacer.frame_samples, offset + 1, dtype=np.int16)
        await pacer.write_pcm16(samples.tobytes())

    first = await track.recv()
    second = await track.recv()
    third = await track.recv()

    assert first.pts == 0
    assert second.pts == pacer.frame_samples
    assert third.pts == pacer.frame_samples * 2
    assert sleeps == pytest.approx([0.02, 0.02])


