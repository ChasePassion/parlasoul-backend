from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import numpy as np
import pytest

from src.realtime.bridge.subtitle_bridge import SubtitleBridge
from src.realtime.pipeline.audio_pacer import AudioPacer
from src.realtime.pipeline.bus import RealtimeEventBus
from src.realtime.pipeline.interrupt_manager import VADTransition
from src.realtime.pipeline.turn_commit_gate import TurnCommitGateConfig
from src.realtime.session.models import CurrentTurnContext, RealtimeSession, SessionConfig
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


class _ControlledInterruptManager:
    def __init__(self) -> None:
        self._transitions: list[VADTransition] = []
        self._state = RealtimeSessionState.LISTENING
        self._getter = None
        self._transition = None

    @property
    def state(self) -> RealtimeSessionState:
        return self._getter() if self._getter is not None else self._state

    def bind_state_controller(self, *, getter, transition) -> None:
        self._getter = getter
        self._transition = transition

    def push(self, transition: VADTransition) -> None:
        self._transitions.append(transition)

    async def ingest_frame(self, _: bytes, *, frame_ms: int) -> VADTransition:
        assert frame_ms == 20
        transition = self._transitions.pop(0) if self._transitions else VADTransition()
        if transition.speech_started:
            await self._transition(RealtimeSessionState.USER_SPEAKING, "vad_speech_started")
        if transition.speech_stopped:
            await self._transition(RealtimeSessionState.ENDPOINT_PENDING, "vad_speech_stopped")
        return transition

    async def maybe_interrupt(self, *, reason: str, generation_id: int | None = None) -> bool:
        del generation_id
        await self._transition(RealtimeSessionState.INTERRUPTING, reason)
        return True


class _FakeMetricsSink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.turns: list[dict[str, object]] = []
        self.closed = asyncio.Event()

    async def record_event(self, **payload: object) -> None:
        self.events.append(dict(payload))

    async def upsert_turn_metrics(self, **payload: object) -> None:
        self.turns.append(dict(payload))

    async def close(self) -> None:
        self.closed.set()


class _FakeSegmenter:
    def feed(self, chunk: str) -> list[str]:
        return [chunk] if chunk else []

    def flush_for_latency(self) -> str | None:
        return None

    def flush(self) -> list[str]:
        return []


class _ControlledVoiceRuntime:
    def __init__(self) -> None:
        self.stt_gate: asyncio.Event | None = None
        self.tts_gate: asyncio.Event | None = None
        self.stt_started = asyncio.Event()
        self.stt_cancelled = asyncio.Event()
        self.tts_started = asyncio.Event()
        self.tts_segment_received = asyncio.Event()
        self.tts_finished = asyncio.Event()
        self.tts_cancelled = asyncio.Event()
        self.frames_per_segment = 3

    async def transcribe_pcm16_mono(self, audio_bytes: bytes, *, sample_rate: int) -> str:
        assert audio_bytes
        assert sample_rate == 16000
        self.stt_started.set()
        try:
            if self.stt_gate is not None:
                await self.stt_gate.wait()
            return "user utterance"
        except asyncio.CancelledError:
            self.stt_cancelled.set()
            raise

    def build_sentence_segmenter(self) -> _FakeSegmenter:
        return _FakeSegmenter()

    async def stream_tts_to_pacer(
        self,
        *,
        segments_queue,
        audio_pacer: AudioPacer,
        generation_id: int,
        **_: object,
    ) -> None:
        self.tts_started.set()
        try:
            while True:
                segment = await segments_queue.get()
                if segment is None:
                    break
                self.tts_segment_received.set()
                if self.tts_gate is not None:
                    await self.tts_gate.wait()
                frame = np.full(audio_pacer.frame_samples, 9, dtype=np.int16).tobytes()
                accepted = await audio_pacer.write_pcm16(
                    frame * self.frames_per_segment,
                    generation_id=generation_id,
                )
                if not accepted:
                    raise asyncio.CancelledError
            self.tts_finished.set()
        except asyncio.CancelledError:
            self.tts_cancelled.set()
            raise


class _ControlledDialogService:
    def __init__(self) -> None:
        self.llm_gate: asyncio.Event | None = None
        self.translation_gate: asyncio.Event | None = None
        self.llm_started = asyncio.Event()
        self.llm_cancelled = asyncio.Event()
        self.translation_started = asyncio.Event()
        self.translation_cancelled = asyncio.Event()

    async def stream_assistant_reply(self, **_: object):
        self.llm_started.set()
        try:
            if self.llm_gate is not None:
                await self.llm_gate.wait()
            yield "assistant response."
        except asyncio.CancelledError:
            self.llm_cancelled.set()
            raise

    async def translate_assistant_chunk(self, _: str) -> str:
        self.translation_started.set()
        try:
            if self.translation_gate is not None:
                await self.translation_gate.wait()
            return "translated response"
        except asyncio.CancelledError:
            self.translation_cancelled.set()
            raise


class _FakeTurnService:
    def __init__(self) -> None:
        self.begin_called = asyncio.Event()
        self.success = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.error = asyncio.Event()
        self.partial_contents: list[str] = []

    async def begin_turn(self, **_: object) -> CurrentTurnContext:
        self.begin_called.set()
        return CurrentTurnContext(
            history_leaf_turn_id=None,
            user_turn_id=uuid.uuid4(),
            user_candidate_id=uuid.uuid4(),
            assistant_turn_id=uuid.uuid4(),
            assistant_candidate_id=uuid.uuid4(),
            raw_user_text="user utterance",
        )

    async def persist_assistant_partial(self, *, content: str, **_: object) -> bool:
        self.partial_contents.append(content)
        return True

    async def finalize_assistant_success(self, *_: object, **__: object) -> None:
        self.success.set()

    async def finalize_assistant_interrupted(self, *_: object, **__: object) -> bool:
        self.interrupted.set()
        return True

    async def finalize_assistant_error(self, *_: object, **__: object) -> None:
        self.error.set()


@dataclass
class _Harness:
    session: RealtimeSession
    pacer: AudioPacer
    bus: RealtimeEventBus
    interrupt: _ControlledInterruptManager
    voice: _ControlledVoiceRuntime
    dialog: _ControlledDialogService
    turns: _FakeTurnService
    metrics: _FakeMetricsSink


def _build_harness(
    *,
    translation_enabled: bool = False,
    interrupt_hard_confirm_ms: int = 0,
) -> _Harness:
    bus = RealtimeEventBus()
    pacer = AudioPacer(
        sample_rate=48000,
        frame_ms=20,
        prebuffer_ms=20,
        fadeout_ms=20,
    )
    interrupt = _ControlledInterruptManager()
    voice = _ControlledVoiceRuntime()
    dialog = _ControlledDialogService()
    turns = _FakeTurnService()
    metrics = _FakeMetricsSink()
    session = RealtimeSession(
        session_id="rt_lifecycle",
        user_id=uuid.uuid4(),
        chat_id=uuid.uuid4(),
        character_id=uuid.uuid4(),
        peer=_FakePeer(),
        bus=bus,
        audio_pacer=pacer,
        event_bridge=_FakeEventBridge(),
        subtitle_bridge=SubtitleBridge(bus),
        interrupt_manager=interrupt,
        dialog_service=dialog,
        turn_service=turns,
        voice_runtime=voice,
        config=SessionConfig(
            translation_enabled=translation_enabled,
            turn_commit=TurnCommitGateConfig(commit_grace_ms=0, min_audio_ms=1),
            interrupt_hard_confirm_ms=interrupt_hard_confirm_ms,
            interrupt_resume_grace_ms=250,
        ),
        metrics_sink=metrics,
    )
    return _Harness(session, pacer, bus, interrupt, voice, dialog, turns, metrics)


def _utterance() -> bytes:
    return np.ones(320, dtype=np.int16).tobytes()


async def _wait_response_finished(session: RealtimeSession) -> None:
    async with asyncio.timeout(1):
        while session._response_task is not None:
            await asyncio.sleep(0)


async def _wait_for_metric(sink: _FakeMetricsSink, event_type: str) -> None:
    async with asyncio.timeout(1):
        while not any(row["event_type"] == event_type for row in sink.events):
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancel_during_stt_awaits_task_and_restores_listening() -> None:
    harness = _build_harness()
    harness.voice.stt_gate = asyncio.Event()

    await harness.session._on_utterance_commit(_utterance(), "vad_silence")
    await harness.voice.stt_started.wait()
    await harness.session.cancel_response("client_response_cancelled", clear_audio=False)

    assert harness.voice.stt_cancelled.is_set()
    assert harness.session._response_task is None
    assert harness.session.state == RealtimeSessionState.LISTENING
    assert not harness.turns.begin_called.is_set()
    assert harness.metrics.turns[-1]["metrics"]["outcome"] == "cancelled"
    await harness.session.close()
    assert not harness.session._tasks
    assert harness.metrics.closed.is_set()




@pytest.mark.asyncio
async def test_response_done_waits_for_pacer_drain_not_translation() -> None:
    harness = _build_harness(translation_enabled=True)
    harness.dialog.translation_gate = asyncio.Event()

    async with harness.bus.subscribe("response_done") as response_done:
        await harness.session._on_utterance_commit(_utterance(), "vad_silence")
        await harness.voice.tts_finished.wait()
        await harness.dialog.translation_started.wait()
        assert response_done.empty()

        for _ in range(harness.voice.frames_per_segment):
            await harness.pacer.read_frame()

        event = await asyncio.wait_for(response_done.get(), timeout=1)

    assert event.payload["generation_id"] == 1
    assert harness.session.state == RealtimeSessionState.LISTENING
    assert not harness.dialog.translation_gate.is_set()
    await _wait_response_finished(harness.session)
    await harness.session.close()
    assert harness.dialog.translation_cancelled.is_set()
    assert not harness.session._tasks


@pytest.mark.asyncio
async def test_user_can_interrupt_while_tts_tail_is_waiting_to_drain() -> None:
    harness = _build_harness()
    harness.interrupt.push(
        VADTransition(
            speech_started=True,
            preroll_audio=_utterance(),
            confidence=0.9,
            voiced_ms_delta=20,
        )
    )

    async with harness.bus.subscribe("response_done") as response_done:
        async with harness.bus.subscribe("response_audio_stopped") as audio_stopped:
            await harness.session._on_utterance_commit(_utterance(), "vad_silence")
            await harness.voice.tts_finished.wait()
            await _wait_for_metric(harness.metrics, "first_audio_enqueued")
            assert harness.session.state == RealtimeSessionState.AGENT_SPEAKING

            await harness.session.process_audio_frame(_utterance(), 16000)
            await harness.turns.interrupted.wait()
            await harness.pacer.read_frame()
            stopped = await asyncio.wait_for(audio_stopped.get(), timeout=1)

            assert stopped.payload["generation_id"] == 1
            assert response_done.empty()

    await _wait_for_metric(harness.metrics, "audio_stopped")
    assert harness.session.state == RealtimeSessionState.USER_SPEAKING
    assert harness.metrics.turns[-1]["metrics"]["outcome"] == "interrupted"
    await harness.session.close()




