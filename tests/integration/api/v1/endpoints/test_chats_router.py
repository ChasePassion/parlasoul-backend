from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.core.dependencies as core_deps
from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import chats
from src.core.dependencies import (
    get_candidate_repository,
    get_character_llm_service,
    get_character_repository,
    get_chat_repository,
    get_chat_service,
    get_saved_item_repository,
    get_turn_repository,
    get_uow,
    get_user_settings_repository,
    get_voice_service,
)
from src.db.models import User


class _StubChatService:
    def __init__(self) -> None:
        self.prepare_stream = AsyncMock(return_value={"prepared": True})
        self.stream_chat_called = False

    async def stream_chat_with_prep(self, **kwargs):
        self.stream_chat_called = True
        yield (
            'data: {"type":"tts_audio_delta","assistant_candidate_id":"a1","seq":1,'
            '"audio_b64":"AQI=","mime_type":"audio/opus"}\n\n'
        )
        yield (
            'data: {"type":"reply_card","assistant_candidate_id":"m1","reply_card":'
            '{"surface":"That sounds great!","zh":"那听起来太棒了！","key_phrases":'
            '[{"surface":"sounds great","ipa_us":"/saʊndz ɡreɪt/","zh":"听起来很棒"},'
            '{"surface":"That sounds","ipa_us":"/ðæt saʊndz/","zh":"这听起来"}],'
            '"favorite":{"enabled":true,"is_favorited":false,"saved_item_id":null}}}\n\n'
        )
        yield 'data: {"type":"done"}\n\n'


def _create_app(service: _StubChatService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(chats.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="u@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_chat_service] = lambda: service
    return app






def test_get_recent_chat_still_works_when_memory_init_fails(monkeypatch) -> None:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(chats.router)

    user = User(id=uuid.uuid4(), email="u@example.com")
    character_id = uuid.uuid4()
    character_repo = SimpleNamespace(
        get_by_id=AsyncMock(
            return_value=SimpleNamespace(
                id=character_id,
                visibility="PUBLIC",
                creator_id=None,
            )
        )
    )
    chat_repo = SimpleNamespace(
        delete_empty_drafts=AsyncMock(return_value=0),
        get_recent_active=AsyncMock(return_value=None),
    )

    async def _override_current_user() -> User:
        return user

    def _boom_memory_system():
        raise ImportError("numpy.core.multiarray failed to import")

    monkeypatch.setattr(core_deps, "get_memory_system", _boom_memory_system)

    @asynccontextmanager
    async def _transactional():
        yield

    uow = SimpleNamespace(transactional=_transactional)

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_chat_repository] = lambda: chat_repo
    app.dependency_overrides[get_turn_repository] = lambda: SimpleNamespace()
    app.dependency_overrides[get_candidate_repository] = lambda: SimpleNamespace()
    app.dependency_overrides[get_character_repository] = lambda: character_repo
    app.dependency_overrides[get_user_settings_repository] = lambda: SimpleNamespace()
    app.dependency_overrides[get_saved_item_repository] = lambda: SimpleNamespace()
    app.dependency_overrides[get_voice_service] = lambda: None
    app.dependency_overrides[get_character_llm_service] = lambda: SimpleNamespace()

    with TestClient(app) as client:
        resp = client.get(f"/v1/chats/recent?character_id={character_id}")

    assert resp.status_code == 200
    assert resp.json()["code"] == "ok"
    assert resp.json()["data"] is None
    character_repo.get_by_id.assert_awaited_once()
    chat_repo.delete_empty_drafts.assert_awaited_once()
    chat_repo.get_recent_active.assert_awaited_once()




def test_stream_chat_route_streams_sse_events() -> None:
    service = _StubChatService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            f"/v1/chats/{uuid.uuid4()}/stream",
            json={"content": "hello"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"type":"tts_audio_delta"' in resp.text
    assert '"type":"reply_card"' in resp.text
    assert '"favorite":{"enabled":true,"is_favorited":false,"saved_item_id":null}' in resp.text
    assert '"type":"done"' in resp.text
    service.prepare_stream.assert_awaited_once()
    assert service.stream_chat_called is True
