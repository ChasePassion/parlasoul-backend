from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import learning
from src.core.dependencies import get_chat_learning_service, get_turn_service
from src.db.models import User


class _StubChatLearningService:
    def __init__(self) -> None:
        self.generate_word_card = AsyncMock(
            return_value={
                "surface": "catch up",
                "ipa_us": "/kætʃ ʌp/",
                "pos_groups": [{"pos": "v.", "senses": [{"zh": "叙旧", "note": None}]}],
                "example": {"surface": "Let's catch up soon.", "zh": "我们尽快叙叙旧。"},
            }
        )
        self.resolve_favorite_state = AsyncMock(
            return_value={
                "enabled": True,
                "is_favorited": False,
                "saved_item_id": None,
            }
        )

    @staticmethod
    def contains_english(text: str) -> bool:
        return bool(text and any("a" <= ch.lower() <= "z" for ch in text))

    @staticmethod
    def attach_word_card_favorite(card_core, favorite):
        payload = card_core.model_dump() if hasattr(card_core, "model_dump") else dict(card_core)
        return {
            **payload,
            "favorite": favorite,
        }


class _StubTurnService:
    def __init__(self) -> None:
        self.generate_reply_card_for_candidate = AsyncMock(
            return_value={
                "reply_card": {
                    "surface": "That sounds great!",
                    "zh": "那听起来太棒了！",
                    "key_phrases": [
                        {"surface": "sounds great", "ipa_us": "/saʊndz ɡreɪt/", "zh": "听起来很棒"},
                        {"surface": "That sounds", "ipa_us": "/ðæt saʊndz/", "zh": "这听起来"},
                    ],
                    "favorite": {
                        "enabled": True,
                        "is_favorited": False,
                        "saved_item_id": None,
                    },
                }
            }
        )


def _create_app(
    service: _StubChatLearningService,
    turn_service: _StubTurnService | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(learning.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="u@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_chat_learning_service] = lambda: service
    if turn_service is None:
        turn_service = _StubTurnService()
    app.dependency_overrides[get_turn_service] = lambda: turn_service
    return app


def test_word_card_route_returns_card_payload() -> None:
    service = _StubChatLearningService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/learning/word-card",
            json={
                "selected_text": "catch up",
                "context_text": "Let's catch up soon.",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "ok"
    assert body["data"]["word_card"]["surface"] == "catch up"
    service.generate_word_card.assert_awaited_once()
    service.resolve_favorite_state.assert_awaited_once()


def test_reply_card_route_returns_card_payload() -> None:
    service = _StubChatLearningService()
    turn_service = _StubTurnService()
    app = _create_app(service, turn_service)

    with TestClient(app) as client:
        resp = client.post(f"/v1/learning/reply-card/candidates/{uuid.uuid4()}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "ok"
    assert body["data"]["reply_card"]["surface"] == "That sounds great!"
    turn_service.generate_reply_card_for_candidate.assert_awaited_once()
