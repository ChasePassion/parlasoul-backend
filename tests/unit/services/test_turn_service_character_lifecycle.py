from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.exceptions import APIError
from src.db.models import (
    AuthorType,
    Character,
    CharacterStatusType,
    Chat,
    ChatState,
    ChatType,
    Turn,
    User,
    VisibilityType,
)
from src.services.turn_service import TurnService


class _AwaitableSpy:
    def __init__(self, result=None):
        self.result = result
        self.await_count = 0

    async def __call__(self, *args, **kwargs):
        self.await_count += 1
        return self.result


def _user(user_id: uuid.UUID | None = None) -> User:
    return User(id=user_id or uuid.uuid4(), email="user@example.com")


def _character(*, status: str = CharacterStatusType.ACTIVE.value) -> Character:
    return Character(
        id=uuid.uuid4(),
        name="Luna",
        description="desc",
        system_prompt="prompt",
        greeting_message="hello",
        avatar_image_key=None,
        voice_provider="dashscope",
        voice_model="qwen3-tts-instruct-flash-realtime",
        voice_provider_voice_id="Cherry",
        voice_source_type="system",
        status=status,
        visibility=VisibilityType.PUBLIC.value,
        creator_id=uuid.uuid4(),
        identifier=None,
        interaction_count=0,
    )


def _chat(*, user_id: uuid.UUID, character_id: uuid.UUID) -> Chat:
    return Chat(
        id=uuid.uuid4(),
        user_id=user_id,
        character_id=character_id,
        type=ChatType.ONE_ON_ONE.value,
        state=ChatState.ACTIVE.value,
        visibility=VisibilityType.PRIVATE.value,
        title="New Chat",
    )


def _turn(*, chat_id: uuid.UUID, author_type: str) -> Turn:
    return Turn(
        id=uuid.uuid4(),
        chat_id=chat_id,
        turn_no=1,
        author_type=author_type,
        state="OK",
        is_proactive=False,
    )


def _service(*, chat_repo, turn_repo, character_repo, candidate_repo=None) -> TurnService:
    return TurnService(
        uow=SimpleNamespace(commit=_AwaitableSpy(), rollback=_AwaitableSpy(), flush=_AwaitableSpy()),
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        candidate_repo=candidate_repo or SimpleNamespace(get_max_candidate_no=_AwaitableSpy()),
        character_repo=character_repo,
        memory_repo=SimpleNamespace(),
        user_settings_repo=SimpleNamespace(),
        chat_learning_service=SimpleNamespace(),
        subscription_service=SimpleNamespace(
            has_feature=lambda *_args, **_kwargs: False,
            assert_feature=lambda *_args, **_kwargs: None,
        ),
        voice_service=None,
        character_llm_service=SimpleNamespace(resolve_runtime_route=_AwaitableSpy()),
    )


@pytest.mark.asyncio
async def test_prepare_regen_rejects_unpublished_character() -> None:
    current_user = _user()
    character = _character(status=CharacterStatusType.UNPUBLISHED.value)
    chat = _chat(user_id=current_user.id, character_id=character.id)
    assistant_turn = _turn(chat_id=chat.id, author_type=AuthorType.CHARACTER.value)
    turn_repo = SimpleNamespace(get_for_update=_AwaitableSpy(assistant_turn))
    chat_repo = SimpleNamespace(get_for_update=_AwaitableSpy(chat))
    character_repo = SimpleNamespace(get_by_id=_AwaitableSpy(character))
    candidate_repo = SimpleNamespace(get_max_candidate_no=Mock())
    service = _service(
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        character_repo=character_repo,
        candidate_repo=candidate_repo,
    )

    with pytest.raises(APIError) as exc:
        await service.prepare_regen(current_user=current_user, assistant_turn_id=assistant_turn.id)

    assert exc.value.status_code == 409
    assert exc.value.code == "resource_conflict"
    candidate_repo.get_max_candidate_no.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_user_edit_stream_rejects_unpublished_character() -> None:
    current_user = _user()
    character = _character(status=CharacterStatusType.UNPUBLISHED.value)
    chat = _chat(user_id=current_user.id, character_id=character.id)
    user_turn = _turn(chat_id=chat.id, author_type=AuthorType.USER.value)
    turn_repo = SimpleNamespace(get_for_update=_AwaitableSpy(user_turn))
    chat_repo = SimpleNamespace(get_for_update=_AwaitableSpy(chat))
    character_repo = SimpleNamespace(get_by_id=_AwaitableSpy(character))
    candidate_repo = SimpleNamespace(get_max_candidate_no=Mock())
    service = _service(
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        character_repo=character_repo,
        candidate_repo=candidate_repo,
    )

    with pytest.raises(APIError) as exc:
        await service.prepare_user_edit_stream(
            current_user=current_user,
            user_turn_id=user_turn.id,
            content="updated content",
        )

    assert exc.value.status_code == 409
    assert exc.value.code == "resource_conflict"
    candidate_repo.get_max_candidate_no.assert_not_called()
