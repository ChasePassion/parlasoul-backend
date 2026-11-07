from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import voice
from src.core.dependencies import get_voice_service
from src.db.models import User
from src.schemas.voice import STTTranscriptionResult


class _StubVoiceService:
    def __init__(self) -> None:
        self.transcribe_audio = AsyncMock(
            return_value=STTTranscriptionResult(
                text="hello world",
                model="fun-asr-realtime",
                request_id="req-1",
            )
        )
        self.get_assistant_candidate_audio_stream = AsyncMock()


def _create_app(service: _StubVoiceService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(voice.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="u@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_voice_service] = lambda: service
    return app


def test_create_stt_transcription_delegates_to_service() -> None:
    service = _StubVoiceService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/voice/stt/transcriptions",
            data={"audio_format": "wav", "sample_rate": "16000"},
            files={"audio": ("test.wav", b"audio-bytes", "audio/wav")},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["text"] == "hello world"
    service.transcribe_audio.assert_awaited_once()
    assert service.transcribe_audio.await_args.kwargs["chat_id"] is None


def test_create_stt_transcription_passes_chat_id_and_user() -> None:
    service = _StubVoiceService()
    app = _create_app(service)
    chat_id = uuid.uuid4()

    with TestClient(app) as client:
        resp = client.post(
            "/v1/voice/stt/transcriptions",
            data={
                "audio_format": "wav",
                "sample_rate": "16000",
                "chat_id": str(chat_id),
            },
            files={"audio": ("test.wav", b"audio-bytes", "audio/wav")},
        )

    assert resp.status_code == 200
    kwargs = service.transcribe_audio.await_args.kwargs
    assert kwargs["chat_id"] == chat_id
    assert isinstance(kwargs["current_user_id"], uuid.UUID)


def test_stream_tts_for_message_delegates_to_service() -> None:
    service = _StubVoiceService()

    async def _audio_stream():
        yield b"\x01\x02"
        yield b"\x03\x04"

    service.get_assistant_candidate_audio_stream = AsyncMock(
        return_value=("audio/opus", _audio_stream())
    )

    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.get(f"/v1/voice/tts/messages/{uuid.uuid4()}/audio")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/opus")
    assert resp.content == b"\x01\x02\x03\x04"
    service.get_assistant_candidate_audio_stream.assert_awaited_once()

