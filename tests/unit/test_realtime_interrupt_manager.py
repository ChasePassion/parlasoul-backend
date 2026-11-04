from __future__ import annotations

import numpy as np
import pytest

from src.realtime.pipeline.audio_pacer import AudioPacer
from src.realtime.pipeline.bus import RealtimeEventBus
from src.realtime.pipeline.interrupt_manager import InterruptManager
from src.realtime.session.state import RealtimeSessionState


class _FakeSileroVADStream:
    def __init__(self, probabilities: list[float]) -> None:
        self._probabilities = iter(probabilities)
        self.curr_sample = 0

    def infer(self, samples: np.ndarray) -> float:
        self.curr_sample += samples.size
        return next(self._probabilities)


@pytest.mark.asyncio
async def test_interrupt_manager_requires_consecutive_silence_before_stop() -> None:
    bus = RealtimeEventBus()
    pacer = AudioPacer(sample_rate=48000)
    manager = InterruptManager(
        bus=bus,
        audio_pacer=pacer,
        vad_stream=_FakeSileroVADStream([0.8, 0.2, 0.2, 0.2]),
        sample_rate=16000,
        vad_threshold=0.5,
        vad_end_threshold_delta=0.15,
        silence_ms=64,
        speech_pad_ms=30,
        preroll_ms=300,
    )

    frame = (np.ones(512, dtype=np.int16) * 1000).tobytes()

    started = await manager.ingest_frame(frame, frame_ms=32)
    assert started.speech_started is True
    assert started.speech_stopped is False
    assert started.preroll_audio != b""
    assert manager.state == RealtimeSessionState.USER_SPEAKING

    middle = await manager.ingest_frame(frame, frame_ms=32)
    assert middle.speech_started is False
    assert middle.speech_stopped is False
    assert manager.state == RealtimeSessionState.USER_SPEAKING

    almost_stopped = await manager.ingest_frame(frame, frame_ms=32)
    assert almost_stopped.speech_started is False
    assert almost_stopped.speech_stopped is False
    assert manager.state == RealtimeSessionState.USER_SPEAKING

    stopped = await manager.ingest_frame(frame, frame_ms=32)
    assert stopped.speech_started is False
    assert stopped.speech_stopped is True
    assert manager.state == RealtimeSessionState.ENDPOINT_PENDING


@pytest.mark.asyncio
async def test_interrupt_manager_allows_confirmed_interrupt_while_user_speaking() -> None:
    bus = RealtimeEventBus()
    pacer = AudioPacer(sample_rate=48000)
    manager = InterruptManager(
        bus=bus,
        audio_pacer=pacer,
        vad_stream=_FakeSileroVADStream([]),
        sample_rate=16000,
    )
    manager.state = RealtimeSessionState.USER_SPEAKING

    interrupted = await manager.maybe_interrupt(reason="user_speech_detected")

    assert interrupted is True
    assert manager.state == RealtimeSessionState.INTERRUPTED
