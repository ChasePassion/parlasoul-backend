# pyright: reportArgumentType=false

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import voices
from src.core.dependencies import get_subscription_service, get_voice_profile_service
from src.core.exceptions import APIError
from src.db.models import User
from src.schemas.voice_profile import (
    VoiceProfileResponse,
)


class _StubVoiceProfileService:
    def __init__(self) -> None:
        self.list_my_voices = AsyncMock()
        self.list_catalog = AsyncMock()
        self.create_clone = AsyncMock()
        self.get_preview_audio_stream = AsyncMock()
        self.get_voice_by_id = AsyncMock()
        self.patch_voice_by_id = AsyncMock()
        self.delete_voice_by_id = AsyncMock()


class _StubSubscriptionService:
    def __init__(self) -> None:
        self.assert_feature = AsyncMock()


def _build_user() -> User:
    return User(id=uuid.uuid4(), email="tester@example.com")


def _build_voice_response() -> VoiceProfileResponse:
    return VoiceProfileResponse(
        id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        provider="dashscope",
        provider_voice_id="dashscope_clone_1",
        provider_model="qwen3-tts-instruct-flash-realtime",
        source_type="clone",
        display_name="My Voice",
        description="desc",
        status="processing",
        provider_status="updating",
        preview_text="你好，我是你的专属音色。",
        preview_audio_url=None,
        language_tags=["zh-CN"],
        metadata={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _create_app(
    service: _StubVoiceProfileService,
    subscription_service: _StubSubscriptionService | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(voices.router)

    async def _override_current_user() -> User:
        return _build_user()

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_voice_profile_service] = lambda: service
    app.dependency_overrides[get_subscription_service] = lambda: (
        subscription_service or _StubSubscriptionService()
    )
    return app




def test_create_voice_clone_route_blocks_when_subscription_missing() -> None:
    service = _StubVoiceProfileService()
    subscription_service = _StubSubscriptionService()
    subscription_service.assert_feature = AsyncMock(
        side_effect=APIError(
            status_code=403,
            code="subscription_required",
            message=(
                "subscription required: feature requires a paid subscription tier; "
                "feature=voice_clone"
            ),
        )
    )
    app = _create_app(service, subscription_service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/voices/clones",
            data={
                "provider": "dashscope",
                "display_name": "My Voice",
                "preview_text": "你好，我是你的专属音色。",
                "source_audio_format": "wav",
            },
            files={"source_audio": ("clone.wav", b"voice-audio", "audio/wav")},
        )

    assert resp.status_code == 403
    assert resp.json()["code"] == "subscription_required"
    service.create_clone.assert_not_awaited()


def test_stream_voice_preview_route_delegates_to_service() -> None:
    service = _StubVoiceProfileService()

    async def _audio_stream():
        yield b"\x01\x02"
        yield b"\x03\x04"

    service.get_preview_audio_stream = AsyncMock(return_value=("audio/mpeg", _audio_stream()))
    app = _create_app(service)
    voice_id = str(uuid.uuid4())

    with TestClient(app) as client:
        resp = client.get(f"/v1/voices/{voice_id}/preview/audio")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert resp.content == b"\x01\x02\x03\x04"
    service.get_preview_audio_stream.assert_awaited_once()


