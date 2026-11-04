from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import memories
from src.api.schemas import (
    ConsolidateResponse,
    DeleteResponse,
    ManageResponse,
    ResetResponse,
    SearchResponse,
)
from src.core.dependencies import (
    get_character_access_service,
    get_memory_repository,
    get_memory_service,
)
from src.db.models import User
from src.repositories.memory_repository import MemoryRepository
from src.services.character_access_service import CharacterAccessService


class _StubMemoryService:
    def __init__(self) -> None:
        self.manage = AsyncMock(return_value=ManageResponse(added_ids=[1], success=True))
        self.search = AsyncMock(return_value=SearchResponse(episodic=[], semantic=[]))
        self.delete = AsyncMock(return_value=DeleteResponse(success=True, deleted_count=1))
        self.reset = AsyncMock(return_value=ResetResponse(success=True, deleted_count=2))
        self.consolidate = AsyncMock(
            return_value=ConsolidateResponse(memories_processed=10, semantic_created=4)
        )


def _create_app(service: _StubMemoryService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(memories.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="user@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_memory_service] = lambda: service
    return app


def test_manage_route_delegates_to_service() -> None:
    service = _StubMemoryService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/memories/manage",
            json={
                "character_id": str(uuid.uuid4()),
                "chat_id": str(uuid.uuid4()),
                "user_text": "hello",
                "assistant_text": "world",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["added_ids"] == [1]
    service.manage.assert_awaited_once()


def test_delete_route_delegates_to_service() -> None:
    service = _StubMemoryService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.delete(f"/v1/memories/123?character_id={uuid.uuid4()}")

    assert resp.status_code == 200
    assert resp.json()["data"]["deleted_count"] == 1
    service.delete.assert_awaited_once()


def test_search_route_delegates_to_service() -> None:
    service = _StubMemoryService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/memories/search",
            json={
                "character_id": str(uuid.uuid4()),
                "chat_id": str(uuid.uuid4()),
                "query": "what do you remember",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["episodic"] == []
    assert resp.json()["data"]["semantic"] == []
    service.search.assert_awaited_once()


def test_reset_route_delegates_to_service_without_hitting_delete_path() -> None:
    service = _StubMemoryService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.request(
            "DELETE",
            "/v1/memories/reset",
            json={"character_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["deleted_count"] == 2
    service.reset.assert_awaited_once()
    service.delete.assert_not_awaited()


def test_consolidate_route_delegates_to_service() -> None:
    service = _StubMemoryService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/memories/consolidate",
            json={"character_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["memories_processed"] == 10
    assert resp.json()["data"]["semantic_created"] == 4
    service.consolidate.assert_awaited_once()


def test_search_route_uses_real_memory_service_dependency_chain() -> None:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(memories.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="user@example.com")

    memory_repo = MemoryRepository.__new__(MemoryRepository)
    memory_repo.search = AsyncMock(return_value={"episodic": [], "semantic": []})
    character_access = CharacterAccessService.__new__(CharacterAccessService)
    character_access.assert_access = AsyncMock()

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_memory_repository] = lambda: memory_repo
    app.dependency_overrides[get_character_access_service] = lambda: character_access

    with TestClient(app) as client:
        resp = client.post(
            "/v1/memories/search",
            json={
                "character_id": str(uuid.uuid4()),
                "chat_id": str(uuid.uuid4()),
                "query": "what do you remember",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["episodic"] == []
    assert resp.json()["data"]["semantic"] == []
    character_access.assert_access.assert_awaited_once()
    memory_repo.search.assert_awaited_once()
