from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import APIError
from src.core.tts_registry import (
    DEFAULT_SYSTEM_TTS_MODEL,
    DEFAULT_SYSTEM_TTS_PROVIDER,
    DEFAULT_SYSTEM_TTS_VOICE_ID,
)
from src.db.models import Character, User, VisibilityType
from src.repositories.character_repository import CharacterRepository
from src.repositories.voice_gateway_repository import VoiceGatewayRepository
from src.repositories.voice_profile_repository import VoiceProfileRepository
from src.schemas.character import CharacterCreate, CharacterUpdate, VisibilityEnum
from src.services.character_service import CharacterService


def _build_subscription_service(
    *,
    tier: str = "free",
    paywall_enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        paywall_enabled=paywall_enabled,
        get_effective_tier=AsyncMock(return_value=tier),
    )


def _build_user(user_id: uuid.UUID | None = None) -> User:
    return User(id=user_id or uuid.uuid4(), email="user@example.com")


def _build_character(
    *,
    character_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    visibility: str = VisibilityType.PRIVATE.value,
) -> Character:
    return Character(
        id=character_id or uuid.uuid4(),
        name="Alice",
        description="desc",
        system_prompt="prompt",
        greeting_message="hi",
        avatar_image_key=None,
        voice_provider=DEFAULT_SYSTEM_TTS_PROVIDER,
        voice_model=DEFAULT_SYSTEM_TTS_MODEL,
        voice_provider_voice_id=DEFAULT_SYSTEM_TTS_VOICE_ID,
        voice_source_type="system",
        visibility=visibility,
        creator_id=creator_id,
        identifier=None,
    )

@pytest.mark.asyncio
async def test_create_character_uses_defaults_and_returns_response() -> None:
    user = _build_user()
    repo = CharacterRepository.__new__(CharacterRepository)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)

    async def create_side_effect(character: Character) -> Character:
        character.id = uuid.uuid4()
        return character

    repo.create = AsyncMock(side_effect=create_side_effect)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
    )
    payload = CharacterCreate(
        name="A",
        description="B",
        system_prompt="C",
        greeting_message=None,
        avatar_image_key=None,
        visibility=VisibilityEnum.PRIVATE,
    )

    result = await service.create_character(payload, user)

    assert result.creator_id == str(user.id)
    assert result.creator_username == user.username
    assert result.visibility == VisibilityType.PRIVATE.value
    created_arg = repo.create.await_args.args[0]
    assert isinstance(created_arg, Character)
    assert created_arg.visibility == VisibilityType.PRIVATE.value
    assert created_arg.voice_provider == DEFAULT_SYSTEM_TTS_PROVIDER
    assert created_arg.voice_model == DEFAULT_SYSTEM_TTS_MODEL
    assert created_arg.voice_provider_voice_id == DEFAULT_SYSTEM_TTS_VOICE_ID
    assert created_arg.voice_source_type == "system"
    assert created_arg.llm_preset_id == "free"


@pytest.mark.asyncio
async def test_create_character_persists_preset() -> None:
    user = _build_user()
    repo = CharacterRepository.__new__(CharacterRepository)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)

    async def create_side_effect(character: Character) -> Character:
        character.id = uuid.uuid4()
        return character

    repo.create = AsyncMock(side_effect=create_side_effect)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
        subscription_service=_build_subscription_service(tier="plus"),
    )
    payload = CharacterCreate(
        name="A",
        description="B",
        system_prompt="C",
        llm_preset_id="flagship",
        visibility=VisibilityEnum.PRIVATE,
    )

    result = await service.create_character(payload, user)

    created_arg = repo.create.await_args.args[0]
    assert created_arg.llm_preset_id == "flagship"
    assert result.llm_preset_id == "flagship"


@pytest.mark.asyncio
async def test_create_character_rejects_flagship_preset_for_free_user() -> None:
    user = _build_user()
    repo = CharacterRepository.__new__(CharacterRepository)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)

    repo.create = AsyncMock()
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
        subscription_service=_build_subscription_service(tier="free"),
    )
    payload = CharacterCreate(
        name="A",
        description="B",
        system_prompt="C",
        llm_preset_id="flagship",
        visibility=VisibilityEnum.PRIVATE,
    )

    with pytest.raises(APIError) as exc:
        await service.create_character(payload, user)

    assert exc.value.status_code == 403
    assert exc.value.code == "subscription_required"
    repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_create_character_allows_flagship_preset_when_paywall_disabled() -> None:
    user = _build_user()
    repo = CharacterRepository.__new__(CharacterRepository)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)

    async def create_side_effect(character: Character) -> Character:
        character.id = uuid.uuid4()
        return character

    repo.create = AsyncMock(side_effect=create_side_effect)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    subscription_service = _build_subscription_service(tier="free", paywall_enabled=False)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
        subscription_service=subscription_service,
    )
    payload = CharacterCreate(
        name="A",
        description="B",
        system_prompt="C",
        llm_preset_id="flagship",
        visibility=VisibilityEnum.PRIVATE,
    )

    result = await service.create_character(payload, user)

    assert result.llm_preset_id == "flagship"
    subscription_service.get_effective_tier.assert_not_called()


@pytest.mark.asyncio
async def test_update_character_persists_preset() -> None:
    owner = _build_user()
    original = _build_character(creator_id=owner.id)
    updated = _build_character(character_id=original.id, creator_id=owner.id)
    updated.llm_preset_id = "flagship"

    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=original)
    repo.update = AsyncMock(return_value=updated)
    repo.count_distinct_users_by_character = AsyncMock(return_value=0)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
        subscription_service=_build_subscription_service(tier="plus"),
    )

    payload = CharacterUpdate(llm_preset_id="flagship")
    result = await service.update_character(original.id, character_in=payload, current_user=owner)

    update_data = repo.update.await_args.args[1]
    assert update_data["llm_preset_id"] == "flagship"
    assert result.llm_preset_id == "flagship"


@pytest.mark.asyncio
async def test_update_character_rejects_null_llm_preset() -> None:
    owner = _build_user()
    original = _build_character(creator_id=owner.id)

    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=original)
    repo.update = AsyncMock()
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
    )

    with pytest.raises(APIError) as exc:
        await service.update_character(
            original.id,
            character_in=CharacterUpdate(llm_preset_id=None),
            current_user=owner,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "validation_failed"
    repo.update.assert_not_called()




@pytest.mark.asyncio
async def test_get_character_private_accessible_without_ownership() -> None:
    owner_id = uuid.uuid4()
    viewer_id = uuid.uuid4()
    character = _build_character(creator_id=owner_id)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    repo.get_creator_username = AsyncMock(return_value="owner_username")
    repo.count_distinct_users_by_character = AsyncMock(return_value=0)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
    )

    result = await service.get_character(character.id, current_user=_build_user(viewer_id))

    assert result is not None
    assert result.id == str(character.id)


@pytest.mark.asyncio
async def test_update_character_converts_visibility_enum_before_repo_call() -> None:
    owner = _build_user()
    original = _build_character(creator_id=owner.id, visibility=VisibilityType.PRIVATE.value)
    updated = _build_character(
        character_id=original.id,
        creator_id=owner.id,
        visibility=VisibilityType.PUBLIC.value,
    )

    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=original)
    repo.update = AsyncMock(return_value=updated)
    repo.count_distinct_users_by_character = AsyncMock(return_value=0)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
    )

    payload = CharacterUpdate(visibility=VisibilityEnum.PUBLIC)
    result = await service.update_character(original.id, character_in=payload, current_user=owner)

    assert result.visibility == VisibilityType.PUBLIC.value
    update_data = repo.update.await_args.args[1]
    assert update_data["visibility"] == VisibilityType.PUBLIC.value


@pytest.mark.asyncio
async def test_unpublish_character_not_found() -> None:
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=None)
    repo.unpublish = AsyncMock()
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
    )

    with pytest.raises(APIError) as exc:
        await service.unpublish_character(uuid.uuid4(), current_user=_build_user())

    assert exc.value.status_code == 404
    assert exc.value.code == "character_not_found"
    repo.unpublish.assert_not_called()


@pytest.mark.asyncio
async def test_delete_character_removes_for_creator() -> None:
    owner = _build_user()
    character = _build_character(creator_id=owner.id)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    repo.delete = AsyncMock()
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(repo, voice_repo, gateway_repo)

    await service.delete_character(character.id, current_user=owner)

    repo.delete.assert_awaited_once_with(character)


@pytest.mark.asyncio
async def test_delete_character_purges_memories_across_users() -> None:
    owner = _build_user()
    character = _build_character(creator_id=owner.id)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    repo.delete = AsyncMock()
    memory_repo = SimpleNamespace(purge_character=AsyncMock(return_value=3))
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
        memory_repo=memory_repo,
    )

    await service.delete_character(character.id, current_user=owner)

    memory_repo.purge_character.assert_awaited_once_with(character_id=str(character.id))
    repo.delete.assert_awaited_once_with(character)


@pytest.mark.asyncio
async def test_delete_character_survives_memory_purge_failure() -> None:
    owner = _build_user()
    character = _build_character(creator_id=owner.id)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    repo.delete = AsyncMock()
    memory_repo = SimpleNamespace(
        purge_character=AsyncMock(side_effect=RuntimeError("milvus down")),
    )
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
        memory_repo=memory_repo,
    )

    await service.delete_character(character.id, current_user=owner)

    repo.delete.assert_awaited_once_with(character)
    memory_repo.purge_character.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_character_rejects_non_creator() -> None:
    owner = _build_user()
    character = _build_character(creator_id=owner.id)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    repo.delete = AsyncMock()
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(repo, voice_repo, gateway_repo)

    with pytest.raises(APIError) as exc:
        await service.delete_character(character.id, current_user=_build_user())

    assert exc.value.status_code == 403
    assert exc.value.code == "character_modify_forbidden"
    repo.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_character_raises_404_for_missing_character() -> None:
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=None)
    repo.delete = AsyncMock()
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(repo, voice_repo, gateway_repo)

    with pytest.raises(APIError) as exc:
        await service.delete_character(uuid.uuid4(), current_user=_build_user())

    assert exc.value.status_code == 404
    assert exc.value.code == "character_not_found"
    repo.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_unpublish_character_marks_character_as_unpublished() -> None:
    owner = _build_user()
    original = _build_character(creator_id=owner.id)
    unpublished = _build_character(
        character_id=original.id,
        creator_id=owner.id,
        visibility=VisibilityType.PRIVATE.value,
    )
    unpublished.unpublished_at = datetime.now(UTC)

    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=original)
    repo.unpublish = AsyncMock(return_value=unpublished)
    repo.count_distinct_users_by_character = AsyncMock(return_value=0)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(
        repo,
        voice_repo,
        gateway_repo,
    )

    result = await service.unpublish_character(original.id, current_user=owner)

    assert result.id == str(original.id)
    assert result.visibility == VisibilityType.PRIVATE.value
    repo.unpublish.assert_awaited_once_with(original)
