from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from src.db.models import VisibilityType
from src.realtime.session import manager as manager_module
from src.realtime.session.manager import RealtimeSessionManager


class _FakePeer:
    async def negotiate(self, **_: object) -> dict[str, str]:
        return {"type": "answer", "sdp": "fake-answer"}

    async def send_json(self, payload: object) -> None:
        _ = payload


class _FakeVADModel:
    def create_stream(self, *, sample_rate: int) -> object:
        return {"sample_rate": sample_rate}

    def warmup(self, *, sample_rate: int) -> None:
        _ = sample_rate


class _FakeIceConfigService:
    def build_server_rtc_configuration(self, *, user_id: uuid.UUID) -> object:
        return {"user_id": str(user_id)}

    def build_client_ice_servers(self, *, user_id: uuid.UUID) -> list[dict[str, object]]:
        return [{"urls": [f"stun:example.invalid?user={user_id}"]}]


def _fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        REALTIME_AUDIO_PACER_PREBUFFER_MS=120,
        REALTIME_AUDIO_PACER_FADEOUT_MS=50,
        REALTIME_VAD_SAMPLE_RATE=16000,
        REALTIME_VAD_THRESHOLD=0.55,
        REALTIME_VAD_END_THRESHOLD_DELTA=0.15,
        REALTIME_VAD_MIN_SILENCE_MS=800,
        REALTIME_VAD_SPEECH_PAD_MS=200,
        REALTIME_VAD_PRE_ROLL_MS=300,
        REALTIME_NEGOTIATION_TIMEOUT_SECONDS=5.0,
    )


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, fake_session_type: type) -> None:
    monkeypatch.setattr(
        manager_module,
        "build_peer",
        lambda **_: (_FakePeer(), object()),
    )
    monkeypatch.setattr(manager_module, "EventBridge", lambda **_: object())
    monkeypatch.setattr(manager_module, "SubtitleBridge", lambda *_: object())
    monkeypatch.setattr(manager_module, "InterruptManager", lambda **_: object())
    monkeypatch.setattr(manager_module, "RealtimeSession", fake_session_type)


def _build_inputs() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    current_user = SimpleNamespace(id=uuid.uuid4())
    character = SimpleNamespace(
        id=uuid.uuid4(),
        visibility=VisibilityType.PUBLIC.value,
    )
    chat = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=current_user.id,
        character_id=character.id,
    )
    return current_user, chat, character


@pytest.mark.asyncio
async def test_create_session_returns_before_background_start_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    allow_start_finish = asyncio.Event()
    start_finished = asyncio.Event()
    created_sessions: list[_BlockingStartSession] = []

    class _BlockingStartSession:
        def __init__(self, **kwargs: object) -> None:
            self.session_id = kwargs["session_id"]
            created_sessions.append(self)

        async def start(self) -> None:
            start_entered.set()
            await allow_start_finish.wait()
            start_finished.set()

        async def close(self) -> None:
            return None

    _patch_runtime(monkeypatch, _BlockingStartSession)

    manager = RealtimeSessionManager(
        dialog_service=object(),
        turn_service=object(),
        voice_runtime=object(),
        ice_config_service=_FakeIceConfigService(),
        vad_model=_FakeVADModel(),
        settings=_fake_settings(),
    )
    current_user, chat, character = _build_inputs()

    session, answer = await asyncio.wait_for(
        manager.create_session(
            current_user=current_user,
            chat=chat,
            character=character,
            offer={"type": "offer", "sdp": "fake-offer"},
        ),
        timeout=0.2,
    )

    assert answer == {"type": "answer", "sdp": "fake-answer"}
    assert created_sessions == [session]
    assert not start_finished.is_set()
    assert await manager.get_session(session.session_id) is session

    await asyncio.wait_for(start_entered.wait(), timeout=0.2)
    assert not start_finished.is_set()

    allow_start_finish.set()
    await asyncio.wait_for(start_finished.wait(), timeout=0.2)
    await manager.close_session(session.session_id)


