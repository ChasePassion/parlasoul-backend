from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import turns
from src.core.dependencies import get_chat_service, get_turn_service
from src.db.models import User


class _StubTurnService:
    def __init__(self) -> None:
        self.select_candidate = AsyncMock(
            return_value={
                "chat_id": str(uuid.uuid4()),
                "turn_id": str(uuid.uuid4()),
                "primary_candidate_id": str(uuid.uuid4()),
                "primary_candidate_no": 2,
                "candidate_count": 3,
                "active_leaf_turn_id": str(uuid.uuid4()),
            }
        )
        self.prepare_regen = AsyncMock(return_value={"prepared": "regen"})
        self.prepare_user_edit_stream = AsyncMock(return_value={"prepared": "edit"})
        self.generate_feedback_card = AsyncMock(
            return_value={
                "feedback_card": {
                    "surface": "I really want to go now.",
                    "zh": "我现在真的很想走了。",
                    "key_phrases": [
                        {"surface": "really want to", "ipa_us": "/ˈriəli wɑnt tə/", "zh": "真的想要"},
                    ],
                    "favorite": {
                        "enabled": True,
                        "is_favorited": False,
                        "saved_item_id": None,
                    },
                }
            }
        )
        self.regen_stream_called = False
        self.user_edit_stream_called = False

    async def stream_regen_with_prep(self, **kwargs):
        self.regen_stream_called = True
        yield (
            'data: {"type":"reply_card","assistant_candidate_id":"m1","reply_card":'
            '{"surface":"That sounds great!","zh":"那听起来太棒了！","key_phrases":'
            '[{"surface":"sounds great","ipa_us":"/saʊndz ɡreɪt/","zh":"听起来很棒"},'
            '{"surface":"That sounds","ipa_us":"/ðæt saʊndz/","zh":"这听起来"}],'
            '"favorite":{"enabled":true,"is_favorited":false,"saved_item_id":null}}}\n\n'
        )
        yield 'data: {"type":"done"}\n\n'

    async def stream_user_edit_with_prep(self, **kwargs):
        self.user_edit_stream_called = True
        yield (
            'data: {"type":"reply_card","assistant_candidate_id":"m2","reply_card":'
            '{"surface":"That sounds great!","zh":"那听起来太棒了！","key_phrases":'
            '[{"surface":"sounds great","ipa_us":"/saʊndz ɡreɪt/","zh":"听起来很棒"},'
            '{"surface":"That sounds","ipa_us":"/ðæt saʊndz/","zh":"这听起来"}],'
            '"favorite":{"enabled":true,"is_favorited":false,"saved_item_id":null}}}\n\n'
        )
        yield 'data: {"type":"done"}\n\n'


class _StubChatService:
    def __init__(self) -> None:
        self.get_turns_page = AsyncMock(
            return_value={
                "chat": {
                    "id": str(uuid.uuid4()),
                    "user_id": str(uuid.uuid4()),
                    "character_id": str(uuid.uuid4()),
                    "type": "ONE_ON_ONE",
                    "state": "ACTIVE",
                    "visibility": "PRIVATE",
                    "origin": "user",
                    "title": "新聊天",
                    "last_turn_at": None,
                    "last_turn_id": None,
                    "last_turn_no": None,
                    "active_leaf_turn_id": None,
                    "last_read_turn_no": None,
                    "created_at": "2026-02-25T00:00:00Z",
                    "updated_at": "2026-02-25T00:00:00Z",
                    "archived_at": None,
                    "meta": None,
                },
                "character": {
                    "id": str(uuid.uuid4()),
                    "name": "Role",
                    "description": "desc",
                    "greeting_message": None,
                    "avatar_image_key": None,
                    "avatar_urls": None,
                    "visibility": "PUBLIC",
                    "creator_id": None,
                    "creator_username": None,
                    "interaction_count": 1,
                },
                "turns": [],
                "next_before_turn_id": None,
                "has_more": False,
            }
        )


def _create_app(
    turn_service: _StubTurnService,
    chat_service: _StubChatService | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(turns.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="u@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_turn_service] = lambda: turn_service
    if chat_service is None:
        chat_service = _StubChatService()
    app.dependency_overrides[get_chat_service] = lambda: chat_service
    return app


def test_select_candidate_route_delegates_to_service() -> None:
    service = _StubTurnService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            f"/v1/turns/{uuid.uuid4()}/select",
            json={"candidate_no": 2},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["primary_candidate_no"] == 2
    service.select_candidate.assert_awaited_once()


def test_select_candidate_snapshot_route_returns_snapshot_and_delegates() -> None:
    turn_service = _StubTurnService()
    chat_service = _StubChatService()
    app = _create_app(turn_service, chat_service)

    with TestClient(app) as client:
        resp = client.post(
            f"/v1/turns/{uuid.uuid4()}/select/snapshot?limit=30&include_learning_data=false",
            json={"candidate_no": 2},
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["select_result"]["primary_candidate_no"] == 2
    assert "snapshot" in body
    turn_service.select_candidate.assert_awaited_once()
    chat_service.get_turns_page.assert_awaited_once()


def test_regen_stream_route_streams_sse_events() -> None:
    service = _StubTurnService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(f"/v1/turns/{uuid.uuid4()}/regen/stream")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"type":"reply_card"' in resp.text
    assert '"favorite":{"enabled":true,"is_favorited":false,"saved_item_id":null}' in resp.text
    assert '"type":"done"' in resp.text
    service.prepare_regen.assert_awaited_once()
    assert service.regen_stream_called is True


def test_edit_stream_route_streams_sse_events() -> None:
    service = _StubTurnService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            f"/v1/turns/{uuid.uuid4()}/edit/stream",
            json={"content": "updated content"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"type":"reply_card"' in resp.text
    assert '"favorite":{"enabled":true,"is_favorited":false,"saved_item_id":null}' in resp.text
    assert '"type":"done"' in resp.text
    service.prepare_user_edit_stream.assert_awaited_once()
    assert service.user_edit_stream_called is True


def test_feedback_card_route_delegates_to_service() -> None:
    service = _StubTurnService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(f"/v1/turns/{uuid.uuid4()}/feedback-card")

    assert resp.status_code == 200
    assert resp.json()["code"] == "ok"
    assert resp.json()["data"]["feedback_card"]["surface"] == "I really want to go now."
    service.generate_feedback_card.assert_awaited_once()
