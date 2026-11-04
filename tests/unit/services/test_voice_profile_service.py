# pyright: reportArgumentType=false

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import APIError
from src.db.models import User, VoiceProfile, VoiceProfileStatus, VoiceSourceType
from src.repositories.character_repository import CharacterRepository
from src.repositories.voice_gateway_repository import VoiceGatewayRepository
from src.repositories.voice_profile_repository import VoiceProfileRepository
from src.services.voice_profile_service import VoiceProfileService


def _build_user(user_id: uuid.UUID | None = None) -> User:
    return User(id=user_id or uuid.uuid4(), email="user@example.com")


def _build_profile(
    *,
    profile_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
    status: str = VoiceProfileStatus.READY.value,
    provider_model: str = "qwen3-tts-instruct-flash-realtime",
) -> VoiceProfile:
    profile_id = profile_id or uuid.uuid4()
    avatar_owner_id = owner_user_id or uuid.uuid4()
    return VoiceProfile(
        id=profile_id,
        owner_user_id=owner_user_id,
        provider="dashscope",
        provider_voice_id="Cherry",
        source_type=VoiceSourceType.SYSTEM.value
        if owner_user_id is None
        else VoiceSourceType.CLONE.value,
        status=status,
        provider_status="active" if status == VoiceProfileStatus.READY.value else "updating",
        provider_model=provider_model,
        display_name="Cherry",
        description=None,
        avatar_image_key=f"images/avatars/voices/{avatar_owner_id}/{profile_id}",
        preview_text="你好，我是示例试听文案。",
        preview_audio_url=None,
        language_tags=["zh-CN"],
        meta={},
    )


def _build_subscription_service() -> SimpleNamespace:
    return SimpleNamespace(assert_feature=AsyncMock())


def _build_uow() -> SimpleNamespace:
    return SimpleNamespace(transactional=lambda: _FakeTransactional())


class _FakeTransactional:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_list_catalog_returns_selectable_items() -> None:
    user = _build_user()
    custom_voice = _build_profile(
        owner_user_id=user.id,
        provider_model="qwen3-tts-vc-realtime-2026-01-15",
    )

    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.list_user_ready_custom = AsyncMock(return_value=[custom_voice])
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    result = await service.list_catalog(
        current_user=user,
        provider="dashscope",
        model="qwen3-tts-instruct-flash-realtime",
        include_system=True,
        include_user_custom=True,
    )

    assert len(result.items) == 20
    assert result.items[0].provider == "dashscope"
    assert result.items[0].source_type == "system"
    assert result.items[0].preview_audio_url == "/media/voices/preview/dashscope/Cherry.mp3"
    assert result.items[-1].source_type == "clone"
    assert result.items[-1].provider_model == "qwen3-tts-vc-realtime-2026-01-15"
    assert result.items[-1].preview_text == "你好，我是示例试听文案。"
    assert result.items[-1].avatar_image_key == custom_voice.avatar_image_key
    assert result.items[-1].avatar_urls is not None
    repo.list_user_ready_custom.assert_awaited_once_with(
        user_id=user.id,
        provider="dashscope",
        provider_model=None,
    )


@pytest.mark.asyncio
async def test_create_clone_updates_profile_from_gateway_result() -> None:
    user = _build_user()
    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)

    async def _create_side_effect(profile: VoiceProfile) -> VoiceProfile:
        profile.id = uuid.uuid4()
        return profile

    repo.create = AsyncMock(side_effect=_create_side_effect)
    gateway.create_voice_clone = AsyncMock(
        return_value={
            "provider_voice_id": "dashscope_clone_abc",
            "provider_status": "updating",
            "provider_model": "qwen3-tts-vc-realtime-2026-01-15",
            "provider_request_id": "req-1",
            "estimated_ready_seconds": 120,
            "preview_audio_url": None,
        }
    )
    subscription_service = _build_subscription_service()
    service = VoiceProfileService(repo, gateway, character_repo, subscription_service, _build_uow())

    result = await service.create_clone(
        current_user=user,
        provider="dashscope",
        display_name="My Voice",
        preview_text="你好，我是新的试听文案。",
        source_audio_bytes=b"sample-audio",
        source_audio_format="wav",
        description="desc",
        avatar_image_key=f"images/avatars/voices/{user.id}/voice-image",
        language_hint="zh-CN",
        idempotency_key=None,
    )

    assert result.voice.provider_voice_id == "dashscope_clone_abc"
    assert result.voice.status == VoiceProfileStatus.PROCESSING.value
    assert result.voice.preview_text == "你好，我是新的试听文案。"
    assert result.voice.avatar_image_key == f"images/avatars/voices/{user.id}/voice-image"
    assert result.voice.provider_model == "qwen3-tts-vc-realtime-2026-01-15"
    assert result.provider_request_id == "req-1"
    subscription_service.assert_feature.assert_awaited_once_with(user, "voice_clone")
    gateway.create_voice_clone.assert_awaited_once()
    repo.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_clone_raises_before_gateway_when_subscription_denied() -> None:
    user = _build_user()
    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.create = AsyncMock()
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway.create_voice_clone = AsyncMock()
    subscription_error = APIError(
        status_code=403,
        code="subscription_required",
        message="subscription required: feature requires a paid subscription tier; feature=voice_clone",
    )
    subscription_service = SimpleNamespace(assert_feature=AsyncMock(side_effect=subscription_error))
    service = VoiceProfileService(repo, gateway, character_repo, subscription_service, _build_uow())

    with pytest.raises(APIError) as exc:
        await service.create_clone(
            current_user=user,
            provider="dashscope",
            display_name="My Voice",
            preview_text="你好，我是新的试听文案。",
            source_audio_bytes=b"sample-audio",
            source_audio_format="wav",
            description="desc",
            avatar_image_key=f"images/avatars/voices/{user.id}/voice-image",
            language_hint="zh-CN",
            idempotency_key=None,
        )

    assert exc.value.code == "subscription_required"
    gateway.create_voice_clone.assert_not_called()
    repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_create_clone_raises_when_gateway_result_has_no_provider_model() -> None:
    user = _build_user()
    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.create = AsyncMock()
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway.create_voice_clone = AsyncMock(
        return_value={
            "provider_voice_id": "dashscope_clone_abc",
            "provider_status": "active",
            "provider_model": None,
            "provider_request_id": "req-1",
            "estimated_ready_seconds": 0,
            "preview_audio_url": None,
        }
    )
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    with pytest.raises(APIError) as exc:
        await service.create_clone(
            current_user=user,
            provider="dashscope",
            display_name="My Voice",
            preview_text="你好，我是新的试听文案。",
            source_audio_bytes=b"sample-audio",
            source_audio_format="wav",
            description="desc",
            avatar_image_key=f"images/avatars/voices/{user.id}/voice-image",
            language_hint="zh-CN",
            idempotency_key=None,
        )

    assert exc.value.status_code == 502
    assert exc.value.code == "voice_provider_invalid_response"
    repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_patch_voice_updates_preview_text() -> None:
    user = _build_user()
    profile = _build_profile(owner_user_id=user.id)
    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    character_repo = CharacterRepository.__new__(CharacterRepository)
    character_repo.list_ids_by_voice_binding = AsyncMock(return_value=[])
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)

    async def _update_side_effect(
        profile: VoiceProfile, update_data: dict[str, object]
    ) -> VoiceProfile:
        for key, value in update_data.items():
            setattr(profile, key, value)
        return profile

    repo.get_by_id_for_owner = AsyncMock(return_value=profile)
    repo.update = AsyncMock(side_effect=_update_side_effect)
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    result = await service.patch_voice_by_id(
        voice_id=profile.id,
        current_user=user,
        patch_data={"preview_text": "  试听文案已更新  "},
    )

    assert result.preview_text == "试听文案已更新"
    repo.update.assert_awaited_once()


async def _preview_audio_gen():
    yield "AQI=", "audio/mpeg"
    yield "AwQ=", "audio/mpeg"


@pytest.mark.asyncio
async def test_get_preview_audio_stream_uses_preview_text_for_ready_clone() -> None:
    user = _build_user()
    profile = _build_profile(owner_user_id=user.id)
    profile.status = VoiceProfileStatus.READY.value
    profile.source_type = VoiceSourceType.CLONE.value
    profile.provider_voice_id = "dashscope_clone_1"
    profile.provider_model = "qwen3-tts-vc-realtime-2026-01-15"
    profile.preview_text = "这是试听文案"

    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.get_by_id_for_owner = AsyncMock(return_value=profile)
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    captured_kwargs: dict[str, object] = {}

    def _stream_tts_audio_base64(**kwargs):
        captured_kwargs.update(kwargs)
        return _preview_audio_gen()

    gateway.stream_tts_audio_base64 = _stream_tts_audio_base64
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    mime, stream = await service.get_preview_audio_stream(
        voice_id=profile.id,
        current_user=user,
        audio_format="mp3",
    )

    chunks = []
    async for chunk in stream:
        chunks.append(chunk)

    assert mime == "audio/mpeg"
    assert b"".join(chunks) == b"\x01\x02\x03\x04"
    assert captured_kwargs["text"] == "这是试听文案"
    assert captured_kwargs["audio_format"] == "mp3"
    assert captured_kwargs["provider_voice_id"] == "dashscope_clone_1"
    assert captured_kwargs["provider_model"] == "qwen3-tts-vc-realtime-2026-01-15"


@pytest.mark.asyncio
async def test_get_preview_audio_stream_raises_when_preview_text_missing() -> None:
    user = _build_user()
    profile = _build_profile(owner_user_id=user.id)
    profile.status = VoiceProfileStatus.READY.value
    profile.source_type = VoiceSourceType.CLONE.value
    profile.preview_text = None

    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.get_by_id_for_owner = AsyncMock(return_value=profile)
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    with pytest.raises(APIError) as exc:
        await service.get_preview_audio_stream(
            voice_id=profile.id,
            current_user=user,
            audio_format="mp3",
        )

    assert exc.value.status_code == 409
    assert exc.value.code == "voice_preview_text_missing"


@pytest.mark.asyncio
async def test_get_voice_by_id_returns_bound_character_ids() -> None:
    user = _build_user()
    profile = _build_profile(owner_user_id=user.id)
    bound_id = uuid.uuid4()

    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.get_by_id_for_owner = AsyncMock(return_value=profile)
    character_repo = CharacterRepository.__new__(CharacterRepository)
    character_repo.list_ids_by_voice_binding = AsyncMock(return_value=[bound_id])
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    result = await service.get_voice_by_id(voice_id=profile.id, current_user=user)

    assert result.bound_character_count == 1
    assert result.bound_character_ids == [str(bound_id)]


@pytest.mark.asyncio
async def test_patch_voice_updates_bound_characters() -> None:
    user = _build_user()
    profile = _build_profile(owner_user_id=user.id)
    current_bound_id = uuid.uuid4()
    next_bound_id = uuid.uuid4()

    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.get_by_id_for_owner = AsyncMock(return_value=profile)
    repo.update = AsyncMock(return_value=profile)
    character_repo = CharacterRepository.__new__(CharacterRepository)
    character_repo.get_by_ids_for_creator = AsyncMock(
        return_value=[type("CharacterStub", (), {"id": next_bound_id})()]
    )
    character_repo.list_ids_by_voice_binding = AsyncMock(
        side_effect=[[current_bound_id], [next_bound_id]]
    )
    character_repo.assign_voice_to_characters = AsyncMock()
    character_repo.reset_voice_to_default_for_characters = AsyncMock()
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    result = await service.patch_voice_by_id(
        voice_id=profile.id,
        current_user=user,
        patch_data={"character_ids": [str(next_bound_id)]},
    )

    character_repo.assign_voice_to_characters.assert_awaited_once_with(
        creator_id=user.id,
        character_ids=[next_bound_id],
        voice_provider=profile.provider,
        voice_model=profile.provider_model,
        voice_provider_voice_id=profile.provider_voice_id,
        voice_source_type=profile.source_type,
    )
    character_repo.reset_voice_to_default_for_characters.assert_awaited_once_with(
        creator_id=user.id,
        character_ids=[current_bound_id],
    )
    assert result.bound_character_ids == [str(next_bound_id)]


@pytest.mark.asyncio
async def test_delete_voice_in_use_raises_conflict() -> None:
    user = _build_user()
    profile = _build_profile(owner_user_id=user.id)
    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.get_by_id_for_owner = AsyncMock(return_value=profile)
    repo.count_referenced_characters = AsyncMock(return_value=2)
    repo.delete = AsyncMock()
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway.delete_voice_clone = AsyncMock()
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    with pytest.raises(APIError) as exc:
        await service.delete_voice_by_id(voice_id=profile.id, current_user=user)

    assert exc.value.status_code == 409
    assert exc.value.code == "voice_profile_in_use"
    repo.delete.assert_not_called()
    gateway.delete_voice_clone.assert_not_called()


@pytest.mark.asyncio
async def test_delete_voice_calls_gateway_then_repo_delete() -> None:
    user = _build_user()
    profile = _build_profile(owner_user_id=user.id)
    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.get_by_id_for_owner = AsyncMock(return_value=profile)
    repo.count_referenced_characters = AsyncMock(return_value=0)
    repo.delete = AsyncMock()
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway.delete_voice_clone = AsyncMock(return_value={"provider_request_id": "req-del-1"})
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    await service.delete_voice_by_id(voice_id=profile.id, current_user=user)

    gateway.delete_voice_clone.assert_awaited_once_with(
        provider=profile.provider,
        provider_voice_id=profile.provider_voice_id,
    )
    repo.delete.assert_awaited_once_with(profile)


@pytest.mark.asyncio
async def test_delete_failed_pending_voice_skips_gateway_and_deletes_local() -> None:
    user = _build_user()
    profile = _build_profile(owner_user_id=user.id, status=VoiceProfileStatus.FAILED.value)
    profile.provider_voice_id = f"pending-{uuid.uuid4().hex}"
    profile.provider_status = "failed"

    repo = VoiceProfileRepository.__new__(VoiceProfileRepository)
    repo.get_by_id_for_owner = AsyncMock(return_value=profile)
    repo.count_referenced_characters = AsyncMock(return_value=0)
    repo.delete = AsyncMock()
    character_repo = CharacterRepository.__new__(CharacterRepository)
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway.delete_voice_clone = AsyncMock()
    service = VoiceProfileService(repo, gateway, character_repo, _build_subscription_service(), _build_uow())

    await service.delete_voice_by_id(voice_id=profile.id, current_user=user)

    gateway.delete_voice_clone.assert_not_called()
    repo.delete.assert_awaited_once_with(profile)
