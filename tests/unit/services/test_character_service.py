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


@pytest.mark.asyncio
async def test_delete_character_keeps_memories() -> None:
    owner = _build_user()
    character = _build_character(creator_id=owner.id)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    repo.soft_delete = AsyncMock()
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

    memory_repo.purge_character.assert_not_awaited()
    repo.soft_delete.assert_awaited_once_with(character)


@pytest.mark.asyncio
async def test_delete_character_rejects_non_creator() -> None:
    owner = _build_user()
    character = _build_character(creator_id=owner.id)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    repo.soft_delete = AsyncMock()
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(repo, voice_repo, gateway_repo)

    with pytest.raises(APIError) as exc:
        await service.delete_character(character.id, current_user=_build_user())

    assert exc.value.status_code == 403
    assert exc.value.code == "character_modify_forbidden"
    repo.soft_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_character_raises_404_for_missing_character() -> None:
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=None)
    repo.soft_delete = AsyncMock()
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(repo, voice_repo, gateway_repo)

    with pytest.raises(APIError) as exc:
        await service.delete_character(uuid.uuid4(), current_user=_build_user())

    assert exc.value.status_code == 404
    assert exc.value.code == "character_not_found"
    repo.soft_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_character_raises_404_when_already_deleted() -> None:
    owner = _build_user()
    character = _build_character(creator_id=owner.id)
    character.deleted_at = datetime.now(UTC)
    repo = CharacterRepository.__new__(CharacterRepository)
    repo.get_by_id = AsyncMock(return_value=character)
    repo.soft_delete = AsyncMock()
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = CharacterService(repo, voice_repo, gateway_repo)

    with pytest.raises(APIError) as exc:
        await service.delete_character(character.id, current_user=owner)

    assert exc.value.status_code == 404
    repo.soft_delete.assert_not_awaited()




def _build_voice_selectable_repo_mock() -> tuple[CharacterRepository, VoiceProfileRepository]:
    repo = CharacterRepository.__new__(CharacterRepository)
    voice_repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    voice_repo.get_ready_clone_by_owner_and_provider_voice = AsyncMock(return_value=None)
    return repo, voice_repo


@pytest.mark.asyncio
async def test_create_character_accepts_extended_dashscope_system_voice() -> None:
    user = _build_user()
    repo, voice_repo = _build_voice_selectable_repo_mock()
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)

    async def create_side_effect(character: Character) -> Character:
        character.id = uuid.uuid4()
        return character

    repo.create = AsyncMock(side_effect=create_side_effect)
    service = CharacterService(repo, voice_repo, gateway_repo)
    payload = CharacterCreate(
        name="阿清",
        description="粤语陪练",
        system_prompt="C" * 200,
        voice_provider_voice_id="Kiki",
    )

    result = await service.create_character(payload, user)

    created_arg = repo.create.await_args.args[0]
    assert created_arg.voice_provider_voice_id == "Kiki"
    assert result.voice is not None
    assert result.voice.display_name == "Kiki"
    assert result.voice.preview_audio_url is None
    assert result.voice.avatar_image_key is None


@pytest.mark.asyncio
async def test_create_character_rejects_voice_outside_official_catalog() -> None:
    user = _build_user()
    repo, voice_repo = _build_voice_selectable_repo_mock()
    gateway_repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    repo.create = AsyncMock()
    service = CharacterService(repo, voice_repo, gateway_repo)
    payload = CharacterCreate(
        name="A",
        description="B",
        system_prompt="C" * 200,
        voice_provider_voice_id="NotARealVoice",
    )

    with pytest.raises(APIError) as exc:
        await service.create_character(payload, user)

    assert exc.value.status_code == 404
    assert exc.value.code == "voice_not_found"
    repo.create.assert_not_called()


