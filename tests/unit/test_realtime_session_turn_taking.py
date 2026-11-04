from __future__ import annotations

import asyncio
import uuid

import numpy as np
import pytest

from src.realtime.bridge.subtitle_bridge import SubtitleBridge
from src.realtime.pipeline.audio_pacer import AudioPacer
from src.realtime.pipeline.bus import RealtimeEventBus
from src.realtime.pipeline.interrupt_manager import VADTransition
from src.realtime.pipeline.turn_commit_gate import TurnCommitGateConfig
from src.realtime.session.models import RealtimeSession, SessionConfig
from src.realtime.session.state import RealtimeSessionState


class _FakePeer:
    async def send_json(self, _: object) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeEventBridge:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _FakeInterruptManager:
    def __init__(self, transitions: list[VADTransition]) -> None:
        self._transitions = iter(transitions)
        self.state = RealtimeSessionState.IDLE

    async def ingest_frame(self, _: bytes, *, frame_ms: int) -> VADTransition:
        _ = frame_ms
        transition = next(self._transitions)
        if transition.speech_started:
            self.state = RealtimeSessionState.USER_SPEAKING
        if transition.speech_stopped:
            self.state = RealtimeSessionState.THINKING
        return transition

    def mark_thinking(self) -> None:
        self.state = RealtimeSessionState.THINKING

    def mark_idle(self) -> None:
        self.state = RealtimeSessionState.IDLE


class _FakeVoiceRuntime:
    def __init__(self) -> None:
        self.transcribed = asyncio.Event()
        self.audio_bytes: bytes | None = None

    async def transcribe_pcm16_mono(self, audio_bytes: bytes, *, sample_rate: int) -> str:
        assert audio_bytes
        assert sample_rate == 16000
        self.audio_bytes = audio_bytes
        self.transcribed.set()
        return ""


def _frame(value: int, *, sample_rate: int = 16000, ms: int = 20) -> bytes:
    return np.full(sample_rate * ms // 1000, value, dtype=np.int16).tobytes()


@pytest.mark.asyncio
async def test_session_waits_for_turn_commit_gate_before_stt() -> None:
    bus = RealtimeEventBus()
    pacer = AudioPacer(sample_rate=48000, frame_ms=20, prebuffer_ms=20)
    voice_runtime = _FakeVoiceRuntime()
    preroll = np.ones(320, dtype=np.int16).tobytes()
    frame = np.ones(320, dtype=np.int16).tobytes()
    interrupt_manager = _FakeInterruptManager(
        [
            VADTransition(speech_started=True, preroll_audio=preroll),
            VADTransition(),
            VADTransition(speech_stopped=True),
        ]
    )
    session = RealtimeSession(
        session_id="rt_test",
        user_id=uuid.uuid4(),
        chat_id=uuid.uuid4(),
        character_id=uuid.uuid4(),
        peer=_FakePeer(),
        bus=bus,
        audio_pacer=pacer,
        event_bridge=_FakeEventBridge(),
        subtitle_bridge=SubtitleBridge(bus),
        interrupt_manager=interrupt_manager,
        dialog_service=object(),
        turn_service=object(),
        voice_runtime=voice_runtime,
        config=SessionConfig(
            turn_commit=TurnCommitGateConfig(commit_grace_ms=20, min_audio_ms=1)
        ),
    )

    await session.process_audio_frame(frame, 16000)
    await session.process_audio_frame(frame, 16000)
    await session.process_audio_frame(frame, 16000)

    assert not voice_runtime.transcribed.is_set()

    await asyncio.wait_for(voice_runtime.transcribed.wait(), timeout=0.2)
    await session.close()


@pytest.mark.asyncio
async def test_session_preserves_audio_when_user_resumes_during_commit_grace() -> None:
    bus = RealtimeEventBus()
    pacer = AudioPacer(sample_rate=48000, frame_ms=20, prebuffer_ms=20)
    voice_runtime = _FakeVoiceRuntime()
    first_preroll = _frame(10)
    first_audio = _frame(1)
    resumed_preroll = _frame(2)
    resumed_audio = _frame(3)
    interrupt_manager = _FakeInterruptManager(
        [
            VADTransition(speech_started=True, preroll_audio=first_preroll),
            VADTransition(),
            VADTransition(speech_stopped=True),
            VADTransition(speech_started=True, preroll_audio=resumed_preroll),
            VADTransition(),
            VADTransition(speech_stopped=True),
        ]
    )
    session = RealtimeSession(
        session_id="rt_test",
        user_id=uuid.uuid4(),
        chat_id=uuid.uuid4(),
        character_id=uuid.uuid4(),
        peer=_FakePeer(),
        bus=bus,
        audio_pacer=pacer,
        event_bridge=_FakeEventBridge(),
        subtitle_bridge=SubtitleBridge(bus),
        interrupt_manager=interrupt_manager,
        dialog_service=object(),
        turn_service=object(),
        voice_runtime=voice_runtime,
        config=SessionConfig(
            turn_commit=TurnCommitGateConfig(commit_grace_ms=80, min_audio_ms=1)
        ),
    )

    await session.process_audio_frame(_frame(99), 16000)
    await session.process_audio_frame(first_audio, 16000)
    await session.process_audio_frame(_frame(0), 16000)
    await session.process_audio_frame(_frame(88), 16000)
    await session.process_audio_frame(resumed_audio, 16000)
    await session.process_audio_frame(_frame(0), 16000)

    await asyncio.wait_for(voice_runtime.transcribed.wait(), timeout=0.3)

    assert voice_runtime.audio_bytes == first_preroll + first_audio + resumed_preroll + resumed_audio
    await session.close()
