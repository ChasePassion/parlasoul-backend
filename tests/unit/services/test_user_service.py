from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.core.exceptions import APIError
from src.db.models import Character, User, VisibilityType
from src.repositories.user_repository import UserRepository
from src.schemas.user import UserProfileUpdate
from src.services.user_service import UserService


class _AwaitableSpy:
    def __init__(self, result=None):
        self.result = result
        self.await_count = 0
        self.await_args = None

    async def __call__(self, *args, **kwargs):
        self.await_count += 1
        self.await_args = SimpleNamespace(args=args, kwargs=kwargs)
        return self.result

    def assert_not_awaited(self) -> None:
        assert self.await_count == 0

    def assert_awaited_once(self) -> None:
        assert self.await_count == 1


def _build_user(user_id: uuid.UUID | None = None) -> User:
    resolved_user_id = user_id or uuid.uuid4()
    return User(
        id=resolved_user_id,
        email="user@example.com",
        username="old_name",
        avatar_image_key=f"images/avatars/users/{resolved_user_id}/old-image",
        created_at=datetime.now(UTC),
    )


def _build_character(creator_id: uuid.UUID) -> Character:
    return Character(
        id=uuid.uuid4(),
        name="Mika",
        description="desc",
        system_prompt="prompt",
        greeting_message="hi",
        avatar_image_key=None,
        visibility=VisibilityType.PUBLIC.value,
        creator_id=creator_id,
        identifier=None,
        interaction_count=0,
    )


def _build_character_repo() -> SimpleNamespace:
    return SimpleNamespace(batch_count_distinct_users=_AwaitableSpy(result={}))


@pytest.mark.asyncio
async def test_update_my_profile_empty_payload_raises() -> None:
    repo = UserRepository.__new__(UserRepository)
    repo.update_profile = _AwaitableSpy()
    service = UserService(repo, _build_character_repo())

    with pytest.raises(APIError) as exc:
        await service.update_my_profile(UserProfileUpdate(), current_user=_build_user())

    assert exc.value.status_code == 400
    assert exc.value.code == "user_profile_update_empty"
    repo.update_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_my_profile_delegates_to_repository() -> None:
    user = _build_user()
    updated_user = _build_user(user.id)
    updated_user.username = "new_name"
    updated_user.avatar_image_key = f"images/avatars/users/{user.id}/new-image"

    repo = UserRepository.__new__(UserRepository)
    repo.update_profile = _AwaitableSpy(result=updated_user)
    service = UserService(repo, _build_character_repo())

    profile = UserProfileUpdate(
        username="new_name",
        avatar_image_key=f"images/avatars/users/{user.id}/new-image",
    )
    result = await service.update_my_profile(profile, current_user=user)

    assert result.username == "new_name"
    assert result.avatar_image_key == f"images/avatars/users/{user.id}/new-image"
    repo.update_profile.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_creator_characters_public_only_when_not_owner() -> None:
    creator_id = uuid.uuid4()
    viewer = _build_user()
    repo = UserRepository.__new__(UserRepository)
    repo.list_creator_characters = _AwaitableSpy(result=[_build_character(creator_id)])
    repo.get_by_id = _AwaitableSpy(result=_build_user(creator_id))
    service = UserService(repo, _build_character_repo())

    result = await service.get_creator_characters(
        creator_id=creator_id,
        skip=0,
        limit=20,
        current_user=viewer,
    )

    assert repo.list_creator_characters.await_args.kwargs["public_only"] is True
    assert result[0].creator_username == "old_name"


@pytest.mark.asyncio
async def test_get_creator_characters_all_when_owner() -> None:
    creator_id = uuid.uuid4()
    owner = _build_user(creator_id)
    repo = UserRepository.__new__(UserRepository)
    repo.list_creator_characters = _AwaitableSpy(result=[_build_character(creator_id)])
    repo.get_by_id = _AwaitableSpy()
    service = UserService(repo, _build_character_repo())

    result = await service.get_creator_characters(
        creator_id=creator_id,
        skip=0,
        limit=20,
        current_user=owner,
    )

    assert repo.list_creator_characters.await_args.kwargs["public_only"] is False
    repo.get_by_id.assert_not_awaited()
    assert result[0].creator_username == owner.username
