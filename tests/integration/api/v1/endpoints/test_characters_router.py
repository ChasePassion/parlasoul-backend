from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import characters
from src.core.dependencies import get_character_service
from src.db.models import User
from src.schemas.character import CharacterResponse


class _StubCharacterService:
    def __init__(self) -> None:
        self.create_character = AsyncMock()
        self.list_my_characters = AsyncMock()
        self.list_market_characters = AsyncMock()
        self.get_character = AsyncMock()
        self.update_character = AsyncMock()
        self.unpublish_character = AsyncMock()
        self.delete_character = AsyncMock()


def _build_user() -> User:
    return User(id=uuid.uuid4(), email="tester@example.com")


def _build_response() -> CharacterResponse:
    return CharacterResponse(
        id=str(uuid.uuid4()),
        name="Mika",
        description="desc",
        system_prompt="prompt",
        greeting_message="hello",
        avatar_image_key=None,
        voice_provider="dashscope",
        voice_model="qwen3-tts-instruct-flash-realtime",
        voice_provider_voice_id="Cherry",
        voice_source_type="system",
        creator_id=str(uuid.uuid4()),
        creator_username="creator",
        visibility="PRIVATE",
        identifier=None,
        interaction_count=0,
    )


def _create_app(service: _StubCharacterService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(characters.router)

    async def _override_current_user() -> User:
        return _build_user()

    async def _override_optional_user() -> User | None:
        return _build_user()

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[deps.get_optional_current_user] = _override_optional_user
    app.dependency_overrides[get_character_service] = lambda: service
    return app


def test_create_character_route_delegates_to_service() -> None:
    service = _StubCharacterService()
    expected = _build_response()
    service.create_character.return_value = expected
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/characters",
            json={
                "name": "Mika",
                "description": "desc",
                "system_prompt": "prompt",
                "greeting_message": "hello",
                "avatar_image_key": None,
                "voice_provider": "dashscope",
                "voice_model": "qwen3-tts-instruct-flash-realtime",
                "voice_provider_voice_id": "Cherry",
                "voice_source_type": "system",
                "visibility": "PRIVATE",
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "ok"
    assert body["status"] == 201
    assert body["data"]["id"] == expected.id
    service.create_character.assert_awaited_once()


def test_unpublish_character_route_returns_200_and_delegates() -> None:
    service = _StubCharacterService()
    expected = _build_response()
    service.unpublish_character.return_value = expected
    app = _create_app(service)
    character_id = str(uuid.uuid4())

    with TestClient(app) as client:
        resp = client.post(f"/v1/characters/{character_id}/unpublish")

    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == expected.id
    service.unpublish_character.assert_awaited_once()


def test_delete_character_route_returns_204_and_delegates() -> None:
    service = _StubCharacterService()
    app = _create_app(service)
    character_id = str(uuid.uuid4())

    with TestClient(app) as client:
        resp = client.delete(f"/v1/characters/{character_id}")

    assert resp.status_code == 204
    service.delete_character.assert_awaited_once()
