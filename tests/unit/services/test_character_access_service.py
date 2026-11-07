from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import APIError
from src.db.models import Character, User, VisibilityType
from src.repositories.character_repository import CharacterRepository
from src.services.character_access_service import CharacterAccessService


def _user(user_id: uuid.UUID | None = None) -> User:
    return User(id=user_id or uuid.uuid4(), email="viewer@example.com")


def _character(
    *,
    character_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    visibility: str = VisibilityType.PUBLIC.value,
) -> Character:
    return Character(
        id=character_id or uuid.uuid4(),
        name="Mika",
        description="desc",
        system_prompt="prompt",
        greeting_message=None,
        avatar_image_key=None,
        visibility=visibility,
        creator_id=creator_id,
        identifier=None,
        interaction_count=0,
    )


def _service_with(character: Character) -> tuple[CharacterAccessService, AsyncMock]:
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    return CharacterAccessService(repo), repo.get_by_id


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
async def test_missing_character_raises_not_found() -> None:
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=None)
    service = CharacterAccessService(repo)

    with pytest.raises(APIError) as exc:
        await service.assert_access(current_user=_user(), character_id=str(uuid.uuid4()))

    assert exc.value.status_code == 404
    assert exc.value.code == "character_not_found"


@pytest.mark.asyncio
async def test_private_character_accessible_by_any_user() -> None:
    # PRIVATE characters are unlisted, not creator-locked: a share link hands
    # out the id, and any authenticated user may resolve it.
    character = _character(
        creator_id=uuid.uuid4(),
        visibility=VisibilityType.PRIVATE.value,
    )
    service, _ = _service_with(character)

    result = await service.assert_access(current_user=_user(), character_id=str(character.id))

    assert result is character


