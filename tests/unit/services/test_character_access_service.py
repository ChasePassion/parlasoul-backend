from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import APIError
from src.db.models import Character, CharacterStatusType, User, VisibilityType
from src.repositories.character_repository import CharacterRepository
from src.services.character_access_service import (
    CharacterAccessService,
    assert_character_chat_writable,
)


def _user(user_id: uuid.UUID | None = None) -> User:
    return User(id=user_id or uuid.uuid4(), email="viewer@example.com")


def _character(
    *,
    character_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    visibility: str = VisibilityType.PUBLIC.value,
    status: str = CharacterStatusType.ACTIVE.value,
) -> Character:
    return Character(
        id=character_id or uuid.uuid4(),
        name="Mika",
        description="desc",
        system_prompt="prompt",
        greeting_message=None,
        avatar_image_key=None,
        status=status,
        visibility=visibility,
        creator_id=creator_id,
        identifier=None,
        interaction_count=0,
    )


@pytest.mark.asyncio
async def test_invalid_character_id_raises_invalid_param() -> None:
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock()
    service = CharacterAccessService(repo)

    with pytest.raises(APIError) as exc:
        await service.assert_access(current_user=_user(), character_id="not-a-uuid")

    assert exc.value.status_code == 400
    assert exc.value.code == "invalid_param"
    repo.get_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_private_character_access_allowed_for_non_owner() -> None:
    owner_id = uuid.uuid4()
    character = _character(creator_id=owner_id, visibility=VisibilityType.PRIVATE.value)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    service = CharacterAccessService(repo)

    result = await service.assert_access(current_user=_user(), character_id=str(character.id))

    assert result is character


@pytest.mark.asyncio
async def test_public_character_access_allowed() -> None:
    character = _character(visibility=VisibilityType.PUBLIC.value)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    service = CharacterAccessService(repo)

    result = await service.assert_access(current_user=_user(), character_id=str(character.id))

    assert result is character


@pytest.mark.asyncio
async def test_unpublished_character_forbidden_for_non_owner() -> None:
    owner_id = uuid.uuid4()
    character = _character(
        creator_id=owner_id,
        visibility=VisibilityType.PUBLIC.value,
        status=CharacterStatusType.UNPUBLISHED.value,
    )
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    service = CharacterAccessService(repo)

    with pytest.raises(APIError) as exc:
        await service.assert_access(current_user=_user(), character_id=str(character.id))

    assert exc.value.status_code == 403
    assert exc.value.code == "character_private_forbidden"


def test_chat_writable_allows_active_character_for_non_creator() -> None:
    character = _character(creator_id=uuid.uuid4())

    assert_character_chat_writable(character, user_id=uuid.uuid4())


def test_chat_writable_allows_creator_of_unpublished_character() -> None:
    creator_id = uuid.uuid4()
    character = _character(
        creator_id=creator_id,
        status=CharacterStatusType.UNPUBLISHED.value,
    )

    assert_character_chat_writable(character, user_id=creator_id)


def test_chat_writable_rejects_non_creator_of_unpublished_character() -> None:
    character = _character(
        creator_id=uuid.uuid4(),
        status=CharacterStatusType.UNPUBLISHED.value,
    )

    with pytest.raises(APIError) as exc:
        assert_character_chat_writable(character, user_id=uuid.uuid4())

    assert exc.value.status_code == 409
    assert exc.value.code == "resource_conflict"
