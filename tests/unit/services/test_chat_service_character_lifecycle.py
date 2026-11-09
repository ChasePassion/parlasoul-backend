from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.exceptions import APIError
from src.db.models import (
    Character,
    Chat,
    ChatState,
    ChatType,
    User,
    VisibilityType,
)
from src.services.chat_service import ChatService


def _user(user_id: uuid.UUID | None = None) -> User:
    return User(id=user_id or uuid.uuid4(), email="user@example.com")


def _character(
    *,
    character_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    visibility: str = VisibilityType.PUBLIC.value,
) -> Character:
    return Character(
        id=character_id or uuid.uuid4(),
        name="Luna",
        description="desc",
        system_prompt="prompt",
        greeting_message="hello",
        avatar_image_key=None,
        voice_provider="dashscope",
        voice_model="qwen3-tts-instruct-flash-realtime",
        voice_provider_voice_id="Cherry",
        voice_source_type="system",
        visibility=visibility,
        creator_id=creator_id,
        identifier=None,
    )


def _chat(*, user_id: uuid.UUID, character_id: uuid.UUID) -> Chat:
    now = datetime.now(UTC)
    return Chat(
        id=uuid.uuid4(),
        user_id=user_id,
        character_id=character_id,
        type=ChatType.ONE_ON_ONE.value,
        state=ChatState.ACTIVE.value,
        visibility=VisibilityType.PRIVATE.value,
        title="New Chat",
        created_at=now,
        updated_at=now,
    )


def _service(
    *,
    chat_repo,
    turn_repo=None,
    candidate_repo=None,
    character_repo=None,
):
    @asynccontextmanager
    async def _transactional():
        yield

    uow = SimpleNamespace(
        transactional=_transactional,
        refresh=AsyncMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    return ChatService(
        uow=uow,
        chat_repo=chat_repo,
        turn_repo=turn_repo or SimpleNamespace(get_by_id=AsyncMock(), get_ancestor_chain=AsyncMock(return_value=([], False))),
        candidate_repo=candidate_repo or SimpleNamespace(count_by_turn_ids=AsyncMock(return_value={})),
        character_repo=character_repo or SimpleNamespace(),
        memory_repo=SimpleNamespace(),
        user_settings_repo=SimpleNamespace(),
        chat_learning_service=SimpleNamespace(),
        subscription_service=SimpleNamespace(
            has_feature=lambda *_args, **_kwargs: False,
            assert_feature=lambda *_args, **_kwargs: None,
        ),
        chat_title_service=SimpleNamespace(),
        growth_service=None,
        voice_service=None,
        character_llm_service=SimpleNamespace(resolve_runtime_route=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_create_chat_allows_creator_of_unpublished_character() -> None:
    current_user = _user()
    character = _character(
        creator_id=current_user.id,
        visibility=VisibilityType.PRIVATE.value,
    )
    chat_repo = SimpleNamespace(add=Mock())
    turn_repo = SimpleNamespace(add=Mock())
    candidate_repo = SimpleNamespace(add=Mock())
    character_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=character))
    service = _service(
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=character_repo,
    )

    result = await service.create_chat(
        current_user=current_user,
        character_id=character.id,
        meta=None,
    )

    assert result.character.visibility == VisibilityType.PRIVATE.value
    chat_repo.add.assert_called_once()
    turn_repo.add.assert_called_once()
    candidate_repo.add.assert_called_once()


@pytest.mark.asyncio
async def test_create_chat_rejects_soft_deleted_character() -> None:
    current_user = _user()
    character = _character(creator_id=current_user.id)
    character.deleted_at = datetime.now(UTC)
    chat_repo = SimpleNamespace(add=Mock())
    character_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=character))
    service = _service(chat_repo=chat_repo, character_repo=character_repo)

    with pytest.raises(APIError) as exc:
        await service.create_chat(
            current_user=current_user,
            character_id=character.id,
            meta=None,
        )

    assert exc.value.status_code == 404
    assert exc.value.code == "character_not_found"
    chat_repo.add.assert_not_called()


@pytest.mark.asyncio
async def test_get_recent_chat_detail_runs_cleanup_for_private_character() -> None:
    current_user = _user()
    character = _character(
        creator_id=uuid.uuid4(),
        visibility=VisibilityType.PRIVATE.value,
    )
    recent_chat = _chat(user_id=current_user.id, character_id=character.id)
    chat_repo = SimpleNamespace(
        get_recent_active=AsyncMock(return_value=recent_chat),
        delete_empty_drafts=AsyncMock(return_value=0),
    )
    character_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=character))
    service = _service(chat_repo=chat_repo, character_repo=character_repo)

    result = await service.get_recent_chat_detail(current_user=current_user, character_id=character.id)

    assert result is not None
    assert result.chat.id == str(recent_chat.id)
    assert result.character.visibility == VisibilityType.PRIVATE.value
    chat_repo.delete_empty_drafts.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_turns_page_returns_unpublished_character_snapshot() -> None:
    current_user = _user()
    character = _character(
        creator_id=uuid.uuid4(),
        visibility=VisibilityType.PRIVATE.value,
    )
    chat = _chat(user_id=current_user.id, character_id=character.id)
    chat_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=chat))
    character_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=character))
    candidate_repo = SimpleNamespace(count_by_turn_ids=AsyncMock(return_value={}))
    service = _service(
        chat_repo=chat_repo,
        character_repo=character_repo,
        candidate_repo=candidate_repo,
    )

    result = await service.get_turns_page(
        current_user=current_user,
        chat_id=chat.id,
        before_turn_id=None,
        limit=20,
    )

    assert result.character.visibility == VisibilityType.PRIVATE.value
    assert result.turns == []


@pytest.mark.asyncio
async def test_list_sidebar_characters_includes_unpublished_status() -> None:
    current_user = _user()
    character = _character(
        creator_id=uuid.uuid4(),
        visibility=VisibilityType.PRIVATE.value,
    )
    chat_repo = SimpleNamespace(
        list_characters_with_chats=AsyncMock(return_value=[(character, "creator", datetime.now(UTC), False)])
    )
    service = _service(chat_repo=chat_repo, character_repo=SimpleNamespace())

    result = await service.list_sidebar_characters(current_user=current_user, skip=0, limit=20)

    assert len(result) == 1
    assert result[0].visibility == VisibilityType.PRIVATE.value
    assert result[0].creator_username == "creator"
