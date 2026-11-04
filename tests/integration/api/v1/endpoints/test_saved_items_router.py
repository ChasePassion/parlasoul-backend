from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import saved_items
from src.core.dependencies import get_saved_item_service
from src.db.models import User


class _StubSavedItemService:
    def __init__(self) -> None:
        self.create_saved_item = AsyncMock(
            return_value={
                "id": str(uuid.uuid4()),
                "kind": "reply_card",
                "display": {"surface": "hello", "zh": "你好"},
                "card": {"surface": "hello", "zh": "你好", "key_phrases": []},
                "source": {
                    "role_id": str(uuid.uuid4()),
                    "chat_id": str(uuid.uuid4()),
                    "message_id": "m1",
                    "turn_id": None,
                    "candidate_id": None,
                    "meta": None,
                },
                "created_at": "2026-02-24T00:00:00Z",
            }
        )
        self.list_saved_items = AsyncMock(
            return_value={
                "items": [],
                "next_cursor": None,
                "has_more": False,
            }
        )
        self.delete_saved_item = AsyncMock(return_value=None)


def _create_app(service: _StubSavedItemService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(saved_items.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="u@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_saved_item_service] = lambda: service
    return app


def test_create_saved_item_route_delegates_to_service() -> None:
    service = _StubSavedItemService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/saved-items",
            json={
                "saved_item": {
                    "kind": "reply_card",
                    "display": {"surface": "hello", "zh": "你好"},
                    "card": {"surface": "hello", "zh": "你好", "key_phrases": []},
                    "source": {
                        "role_id": str(uuid.uuid4()),
                        "chat_id": str(uuid.uuid4()),
                        "message_id": "m1",
                    },
                }
            },
        )

    assert resp.status_code == 201
    assert resp.json()["code"] == "ok"
    service.create_saved_item.assert_awaited_once()


def test_list_saved_items_route_delegates_to_service() -> None:
    service = _StubSavedItemService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.get("/v1/saved-items?limit=20")

    assert resp.status_code == 200
    assert resp.json()["code"] == "ok"
    service.list_saved_items.assert_awaited_once()


def test_delete_saved_item_route_delegates_to_service() -> None:
    service = _StubSavedItemService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.delete(f"/v1/saved-items/{uuid.uuid4()}")

    assert resp.status_code == 204
    service.delete_saved_item.assert_awaited_once()
