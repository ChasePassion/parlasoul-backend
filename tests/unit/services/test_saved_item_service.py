from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import APIError
from src.db.models import SavedItem, User
from src.repositories.saved_item_repository import SavedItemRepository
from src.schemas.learning import SavedItemCreateRequest
from src.services.saved_item_service import SavedItemService, _saved_item_response


def _build_user() -> User:
    return User(id=uuid.uuid4(), email="user@example.com")


def _build_saved_item(user_id: uuid.UUID) -> SavedItem:
    return SavedItem(
        id=uuid.uuid4(),
        user_id=user_id,
        kind="reply_card",
        display_surface="Hello there",
        display_zh="你好呀",
        card={"surface": "Hello there", "zh": "你好呀", "key_phrases": []},
        source_role_id=uuid.uuid4(),
        source_chat_id=uuid.uuid4(),
        source_message_id=str(uuid.uuid4()),
        source_turn_id=None,
        source_candidate_id=None,
        source_meta=None,
        created_at=datetime.now(UTC),
    )


class _StubUow:
    @asynccontextmanager
    async def transactional(self):
        yield


@pytest.mark.asyncio
async def test_create_saved_item_idempotent_returns_existing() -> None:
    user = _build_user()
    duplicate = _build_saved_item(user.id)

    repo = SavedItemRepository.__new__(SavedItemRepository)
    repo.find_duplicate_by_surface = AsyncMock(return_value=duplicate)
    repo.create = AsyncMock()
    service = SavedItemService(repo, _StubUow())

    request = SavedItemCreateRequest(
        saved_item={
            "kind": "reply_card",
            "display": {"surface": "A", "zh": "B"},
            "card": {"surface": "A", "zh": "B", "key_phrases": []},
            "source": {
                "role_id": str(uuid.uuid4()),
                "chat_id": str(uuid.uuid4()),
                "message_id": "m1",
            },
        }
    )
    response = await service.create_saved_item(request=request, current_user=user)

    assert response.id == str(duplicate.id)
    assert response.kind == duplicate.kind
    repo.create.assert_not_called()
    repo.find_duplicate_by_surface.assert_called_once()


@pytest.mark.asyncio
async def test_delete_saved_item_not_found_raises() -> None:
    user = _build_user()
    repo = SavedItemRepository.__new__(SavedItemRepository)
    repo.get_by_id_and_user = AsyncMock(return_value=None)
    repo.delete = AsyncMock()

    service = SavedItemService(repo, _StubUow())

    with pytest.raises(APIError) as exc:
        await service.delete_saved_item(saved_item_id=str(uuid.uuid4()), current_user=user)

    assert exc.value.status_code == 404
    assert exc.value.code == "saved_item_not_found"
    repo.delete.assert_not_called()


def test_saved_item_response_removes_sentence_level_ipa() -> None:
    user = _build_user()
    item = _build_saved_item(user.id)
    item.card = {
        "ipa_us": "/həˈloʊ/",
        "surface": "Hello there",
        "zh": "你好呀",
        "key_phrases": [
            {"surface": "hello there", "zh": "你好呀", "ipa_us": "/həˈloʊ ðer/"},
        ],
    }

    response = _saved_item_response(item)

    assert "ipa_us" not in response.card
    assert response.card["key_phrases"][0]["ipa_us"] == "/həˈloʊ ðer/"


@pytest.mark.asyncio
async def test_create_saved_item_sanitizes_card_before_persisting() -> None:
    user = _build_user()
    repo = SavedItemRepository.__new__(SavedItemRepository)
    repo.find_duplicate_by_surface = AsyncMock(return_value=None)

    async def _create(item: SavedItem) -> SavedItem:
        item.id = uuid.uuid4()
        item.created_at = datetime.now(UTC)
        return item

    repo.create = AsyncMock(side_effect=_create)
    service = SavedItemService(repo, _StubUow())

    request = SavedItemCreateRequest(
        saved_item={
            "kind": "reply_card",
            "display": {"surface": "A", "zh": "B"},
            "card": {
                "ipa_us": "/top-level/",
                "surface": "A",
                "zh": "B",
                "key_phrases": [
                    {"surface": "A", "zh": "B", "ipa_us": "/phrase/"},
                ],
            },
            "source": {
                "role_id": str(uuid.uuid4()),
                "chat_id": str(uuid.uuid4()),
                "message_id": "m2",
            },
        }
    )

    response = await service.create_saved_item(request=request, current_user=user)
    created_item = repo.create.await_args.args[0]

    assert "ipa_us" not in created_item.card
    assert created_item.card["key_phrases"][0]["ipa_us"] == "/phrase/"
    assert "ipa_us" not in response.card
    assert response.card["key_phrases"][0]["ipa_us"] == "/phrase/"


@pytest.mark.asyncio
async def test_create_saved_item_derives_word_card_display_from_card() -> None:
    user = _build_user()
    repo = SavedItemRepository.__new__(SavedItemRepository)
    repo.find_duplicate_by_surface = AsyncMock(return_value=None)

    async def _create(item: SavedItem) -> SavedItem:
        item.id = uuid.uuid4()
        item.created_at = datetime.now(UTC)
        return item

    repo.create = AsyncMock(side_effect=_create)
    service = SavedItemService(repo, _StubUow())

    request = SavedItemCreateRequest(
        saved_item={
            "kind": "word_card",
            "display": {"surface": "ignored", "zh": "ignored"},
            "card": {
                "surface": "catch up",
                "ipa_us": "/kætʃ ʌp/",
                "pos_groups": [
                    {"pos": "v.", "senses": [{"zh": "叙旧", "note": None}]},
                ],
                "example": {"surface": "Let's catch up soon.", "zh": "我们尽快叙叙旧。"},
            },
            "source": {
                "role_id": str(uuid.uuid4()),
                "chat_id": str(uuid.uuid4()),
                "message_id": "m3",
            },
        }
    )

    response = await service.create_saved_item(request=request, current_user=user)
    created_item = repo.create.await_args.args[0]

    assert created_item.display_surface == "catch up"
    assert created_item.display_zh == "叙旧"
    assert created_item.card["ipa_us"] == "/kætʃ ʌp/"
    assert response.display.surface == "catch up"
    assert response.display.zh == "叙旧"
