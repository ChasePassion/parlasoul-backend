from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.models import Character, ChatState, ChatType, User, VisibilityType
from src.services.chat_service import ChatService


def _user() -> User:
    return User(id=uuid.uuid4(), email="user@example.com")


def _character() -> Character:
    return Character(
        id=uuid.uuid4(),
        name="Luna",
        description="desc",
        system_prompt="prompt",
        greeting_message="hello",
        avatar_image_key=None,
        voice_provider="minimax",
        voice_model="speech-2.8-turbo",
        voice_provider_voice_id="voice-1",
        voice_source_type="system",
        status=VisibilityType.PUBLIC.value,
        visibility=VisibilityType.PUBLIC.value,
        creator_id=uuid.uuid4(),
        identifier=None,
    )


def _service():
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
    chat_repo = SimpleNamespace(
        add=MagicMock(),
        archive_stale_unreplied_proactive_chats=AsyncMock(return_value=1),
    )
    turn_repo = SimpleNamespace(add=MagicMock())
    candidate_repo = SimpleNamespace(add=MagicMock())
    service = ChatService(
        uow=uow,
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=SimpleNamespace(),
        memory_repo=SimpleNamespace(),
        user_settings_repo=SimpleNamespace(),
        chat_learning_service=SimpleNamespace(),
        chat_title_service=SimpleNamespace(),
        growth_service=None,
        voice_service=None,
        character_llm_service=SimpleNamespace(),
        subscription_service=SimpleNamespace(),
    )
    return service, chat_repo, turn_repo, candidate_repo


@pytest.mark.asyncio
async def test_create_proactive_chat_sets_origin_and_source() -> None:
    service, chat_repo, turn_repo, candidate_repo = _service()
    current_user = _user()
    character = _character()

    chat, turn, candidate = await service.create_proactive_chat(
        current_user=current_user,
        character=character,
        content="Hello from proactive outreach",
    )

    assert chat.user_id == current_user.id
    assert chat.character_id == character.id
    assert chat.type == ChatType.ONE_ON_ONE.value
    assert chat.state == ChatState.ACTIVE.value
    assert chat.origin == "proactive"
    assert turn.is_proactive is True
    assert turn.author_character_id == character.id
    assert candidate.extra == {"source": "proactive_outreach"}
    chat_repo.add.assert_called_once()
    turn_repo.add.assert_called_once()
    candidate_repo.add.assert_called_once()
    chat_repo.archive_stale_unreplied_proactive_chats.assert_awaited_once()
