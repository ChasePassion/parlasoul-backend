"""Character service with business rules and orchestration."""

from __future__ import annotations

import logging
import uuid

from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.core.llm_presets import PAID_LLM_PRESET_IDS
from src.core.tts_registry import TTSRegistry
from src.core.tts_registry import tts_registry as default_tts_registry
from src.core.tts_voice_catalog import legacy_dashscope_voice_id_for_minimax
from src.core.voice_profiles import normalize_voice_profile
from src.db.models import (
    Character,
    User,
    VoiceProfile,
    VoiceSourceType,
)
from src.repositories.character_repository import CharacterRepository
from src.repositories.memory_repository import MemoryRepository
from src.repositories.voice_gateway_repository import VoiceGatewayRepository
from src.repositories.voice_profile_repository import VoiceProfileRepository
from src.schemas.character import CharacterCreate, CharacterResponse, CharacterUpdate
from src.schemas.media import MediaUploadKind
from src.schemas.voice_profile import VoiceSelectableItem
from src.services.media_url_service import (
    build_avatar_urls,
    validate_avatar_image_key_owner,
)
from src.services.subscription_service import SubscriptionService


def _to_voice_item(voice: VoiceProfile | None) -> VoiceSelectableItem | None:
    if not voice:
        return None
    usage_hint = None
    if isinstance(voice.meta, dict):
        raw = voice.meta.get("usage_hint")
        if isinstance(raw, str):
            usage_hint = raw
    return VoiceSelectableItem(
        id=str(voice.id),
        display_name=voice.display_name,
        source_type=voice.source_type,
        provider=voice.provider,
        provider_model=voice.provider_model,
        provider_voice_id=voice.provider_voice_id,
        avatar_image_key=voice.avatar_image_key,
        avatar_urls=build_avatar_urls(voice.avatar_image_key),
        preview_text=voice.preview_text,
        preview_audio_url=voice.preview_audio_url,
        usage_hint=usage_hint,
    )


def _to_fallback_voice_item(character: Character) -> VoiceSelectableItem:
    return VoiceSelectableItem(
        id=(
            f"sys:{character.voice_provider}:{character.voice_model}:{character.voice_provider_voice_id}"
            if character.voice_source_type == VoiceSourceType.SYSTEM.value
            else f"clone:{character.voice_provider}:{character.voice_provider_voice_id}"
        ),
        display_name=character.voice_provider_voice_id,
        source_type=character.voice_source_type,
        provider=character.voice_provider,
        provider_model=character.voice_model,
        provider_voice_id=character.voice_provider_voice_id,
    )


def _system_catalog_item_to_voice_item(
    *,
    provider: str,
    model: str,
    item: dict[str, str | None],
) -> VoiceSelectableItem:
    provider_voice_id = item.get("provider_voice_id") or ""
    display_name = item.get("display_name") or provider_voice_id
    usage_hint = item.get("usage_hint")
    avatar_key = item.get("avatar_image_key")
    return VoiceSelectableItem(
        id=f"sys:{provider}:{model}:{provider_voice_id}",
        display_name=display_name,
        source_type=VoiceSourceType.SYSTEM.value,
        provider=provider,
        provider_model=model,
        provider_voice_id=provider_voice_id,
        avatar_image_key=avatar_key,
        avatar_urls=build_avatar_urls(avatar_key),
        preview_audio_url=item.get("preview_audio_url"),
        usage_hint=usage_hint,
    )


def _to_response(
    character: Character,
    voice: VoiceSelectableItem | None,
    *,
    distinct_user_count: int = 0,
    creator_username: str | None = None,
) -> CharacterResponse:
    return CharacterResponse(
        id=str(character.id),
        name=character.name,
        description=character.description,
        system_prompt=character.system_prompt,
        greeting_message=character.greeting_message,
        avatar_image_key=character.avatar_image_key,
        avatar_urls=build_avatar_urls(character.avatar_image_key),
        voice_provider=character.voice_provider,
        voice_model=character.voice_model,
        voice_provider_voice_id=character.voice_provider_voice_id,
        voice_source_type=character.voice_source_type,
        voice_profile=character.voice_profile or None,
        voice=voice,
        llm_preset_id=character.llm_preset_id,
        creator_id=str(character.creator_id) if character.creator_id else None,
        creator_username=creator_username,
        unpublished_at=character.unpublished_at,
        visibility=character.visibility,
        identifier=character.identifier,
        distinct_user_count=distinct_user_count,
    )


def _validate_character_avatar_image_key_or_raise(
    image_key: str | None,
    *,
    current_user: User,
) -> str | None:
    if image_key is None:
        return None
    normalized = validate_avatar_image_key_owner(
        image_key,
        owner_id=str(current_user.id),
        allowed_kinds=(MediaUploadKind.CHARACTER_AVATAR,),
    )
    if normalized is None:
        raise APIError(
            status_code=422,
            code=ErrorCode.VALIDATION_FAILED,
            message="validation failed: avatar image key is not owned by current user",
        )
    return normalized


logger = logging.getLogger(__name__)


class CharacterService:
    """Business logic for character operations."""

    def __init__(
        self,
        repo: CharacterRepository,
        voice_profile_repo: VoiceProfileRepository,
        voice_gateway_repo: VoiceGatewayRepository,
        subscription_service: SubscriptionService | None = None,
        memory_repo: MemoryRepository | None = None,
        *,
        tts_registry: TTSRegistry | None = None,
    ):
        self._repo = repo
        self._voice_profile_repo = voice_profile_repo
        self._voice_gateway_repo = voice_gateway_repo
        self._subscription_service = subscription_service
        self._memory_repo = memory_repo
        self._tts_registry = tts_registry or default_tts_registry

    async def _require_character(self, character_id: uuid.UUID) -> Character:
        character = await self._repo.get_by_id(character_id)
        if not character:
            raise APIError(
                status_code=404,
                code="character_not_found",
                message=f"resource not found: character does not exist; character_id={character_id}",
            )
        return character

    def _assert_creator(self, character: Character, current_user: User, action: str) -> None:
        if character.creator_id != current_user.id:
            raise APIError(
                status_code=403,
                code="character_modify_forbidden",
                message=(
                    f"authorization failed: only creator can {action} character; "
                    f"character_id={character.id}"
                ),
            )

    def _normalize_legacy_system_voice(
        self,
        *,
        provider: str,
        model: str,
        provider_voice_id: str,
    ) -> tuple[str, str, str]:
        if (
            provider.strip().lower() == "minimax"
            and self._tts_registry.router.system_provider == "dashscope"
        ):
            mapped = legacy_dashscope_voice_id_for_minimax(provider_voice_id.strip())
            if mapped:
                profile = self._tts_registry.get_profile(
                    self._tts_registry.router.system_provider
                )
                return (
                    self._tts_registry.router.system_provider,
                    profile.default_model_id if profile else model,
                    mapped,
                )
        return provider, model, provider_voice_id

    async def _get_system_voice_or_raise(
        self,
        *,
        provider: str,
        model: str,
        provider_voice_id: str,
    ) -> VoiceSelectableItem:
        provider, model, provider_voice_id = self._normalize_legacy_system_voice(
            provider=provider,
            model=model,
            provider_voice_id=provider_voice_id,
        )
        voices = self._tts_registry.list_system_voices(
            provider=provider,
            model=model,
        )
        for item in voices:
            current_id = item.get("provider_voice_id")
            if isinstance(current_id, str) and current_id == provider_voice_id:
                return _system_catalog_item_to_voice_item(
                    provider=provider,
                    model=model,
                    item=item,
                )

        raise APIError(
            status_code=404,
            code="voice_not_found",
            message=(
                "resource not found: system voice is not available for model; "
                f"provider={provider}; model={model}; voice_id={provider_voice_id}"
            ),
        )

    async def _get_clone_voice_or_raise(
        self,
        *,
        current_user: User,
        provider: str,
        provider_voice_id: str,
    ) -> VoiceProfile:
        clone_voice = await self._voice_profile_repo.get_ready_clone_by_owner_and_provider_voice(
            owner_user_id=current_user.id,
            provider=provider,
            provider_voice_id=provider_voice_id,
        )
        if not clone_voice:
            raise APIError(
                status_code=409,
                code="voice_profile_not_selectable",
                message=(
                    "resource conflict: clone voice is not selectable for current user; "
                    f"provider={provider}; voice_id={provider_voice_id}"
                ),
            )
        return clone_voice

    async def _resolve_selected_voice_or_raise(
        self,
        *,
        current_user: User,
        voice_provider: str,
        voice_model: str,
        voice_provider_voice_id: str,
        voice_source_type: str,
    ) -> tuple[dict[str, str], VoiceSelectableItem]:
        source_type = voice_source_type.strip().lower()
        provider = voice_provider.strip().lower()
        model = voice_model.strip()
        provider_voice_id = voice_provider_voice_id.strip()
        if not provider or not model or not provider_voice_id:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message=(
                    "validation failed: voice_provider, voice_model, voice_provider_voice_id are required"
                ),
            )

        if source_type == VoiceSourceType.SYSTEM.value:
            selected = await self._get_system_voice_or_raise(
                provider=provider,
                model=model,
                provider_voice_id=provider_voice_id,
            )
            return (
                {
                    "voice_provider": selected.provider,
                    "voice_model": selected.provider_model or model,
                    "voice_provider_voice_id": selected.provider_voice_id,
                    "voice_source_type": VoiceSourceType.SYSTEM.value,
                },
                selected,
            )

        if source_type == VoiceSourceType.CLONE.value:
            clone_voice = await self._get_clone_voice_or_raise(
                current_user=current_user,
                provider=provider,
                provider_voice_id=provider_voice_id,
            )
            selected = _to_voice_item(clone_voice)
            if not selected:
                selected = VoiceSelectableItem(
                    id=f"clone:{provider}:{provider_voice_id}",
                    display_name=provider_voice_id,
                    source_type=VoiceSourceType.CLONE.value,
                    provider=provider,
                    provider_model=clone_voice.provider_model or model,
                    provider_voice_id=provider_voice_id,
                    avatar_image_key=clone_voice.avatar_image_key,
                    avatar_urls=build_avatar_urls(clone_voice.avatar_image_key),
                )
            return (
                {
                    "voice_provider": provider,
                    "voice_model": clone_voice.provider_model or model,
                    "voice_provider_voice_id": provider_voice_id,
                    "voice_source_type": VoiceSourceType.CLONE.value,
                },
                selected,
            )

        raise APIError(
            status_code=422,
            code="validation_failed",
            message=(
                "validation failed: unsupported voice_source_type; "
                f"voice_source_type={voice_source_type}"
            ),
        )

    async def _resolve_character_voice_item(self, character: Character) -> VoiceSelectableItem:
        if (
            character.voice_source_type == VoiceSourceType.CLONE.value
            and character.creator_id is not None
        ):
            clone_voice = (
                await self._voice_profile_repo.get_ready_clone_by_owner_and_provider_voice(
                    owner_user_id=character.creator_id,
                    provider=character.voice_provider,
                    provider_voice_id=character.voice_provider_voice_id,
                )
            )
            item = _to_voice_item(clone_voice)
            if item:
                return item
        if character.voice_source_type == VoiceSourceType.SYSTEM.value:
            provider, model, provider_voice_id = self._normalize_legacy_system_voice(
                provider=character.voice_provider,
                model=character.voice_model,
                provider_voice_id=character.voice_provider_voice_id,
            )
            try:
                voices = self._tts_registry.list_system_voices(
                    provider=provider,
                    model=model,
                )
            except APIError:
                voices = []
            for voice in voices:
                if voice.get("provider_voice_id") == provider_voice_id:
                    return _system_catalog_item_to_voice_item(
                        provider=provider,
                        model=model,
                        item=voice,
                    )
        return _to_fallback_voice_item(character)

    async def _assert_llm_preset_allowed(
        self, *, preset_id: str | None, current_user: User
    ) -> None:
        if preset_id not in PAID_LLM_PRESET_IDS:
            return
        if self._subscription_service is None:
            raise APIError(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="internal error: subscription service is not configured for llm preset validation",
            )
        if not self._subscription_service.paywall_enabled:
            return
        tier = await self._subscription_service.get_effective_tier(current_user)
        if tier == "free":
            raise APIError(
                status_code=403,
                code="subscription_required",
                message=(
                    "subscription required: llm preset requires a paid subscription tier; "
                    f"llm_preset_id={preset_id}"
                ),
            )

    async def _apply_update_llm_binding_or_raise(
        self,
        *,
        update_data: dict[str, object],
        current_user: User,
    ) -> None:
        if "llm_preset_id" not in update_data:
            return
        raw_preset_id = update_data["llm_preset_id"]
        if raw_preset_id is None:
            raise APIError(
                status_code=422,
                code=ErrorCode.VALIDATION_FAILED,
                message="validation failed: llm_preset_id cannot be null; omit it to keep unchanged",
            )
        await self._assert_llm_preset_allowed(
            preset_id=str(raw_preset_id), current_user=current_user
        )

    async def create_character(
        self,
        character_in: CharacterCreate,
        current_user: User,
    ) -> CharacterResponse:
        voice_binding, selected_voice = await self._resolve_selected_voice_or_raise(
            current_user=current_user,
            voice_provider=character_in.voice_provider,
            voice_model=character_in.voice_model,
            voice_provider_voice_id=character_in.voice_provider_voice_id,
            voice_source_type=character_in.voice_source_type.value,
        )
        llm_binding = {"llm_preset_id": character_in.llm_preset_id}
        await self._assert_llm_preset_allowed(
            preset_id=llm_binding["llm_preset_id"],
            current_user=current_user,
        )
        avatar_image_key = _validate_character_avatar_image_key_or_raise(
            character_in.avatar_image_key,
            current_user=current_user,
        )
        voice_profile = normalize_voice_profile(character_in.voice_profile).to_dict()
        if voice_profile and not voice_profile.get("base_voice_id"):
            voice_profile["base_voice_id"] = voice_binding["voice_provider_voice_id"]
        character = Character(
            name=character_in.name,
            description=character_in.description,
            system_prompt=character_in.system_prompt,
            greeting_message=character_in.greeting_message,
            avatar_image_key=avatar_image_key,
            voice_provider=voice_binding["voice_provider"],
            voice_model=voice_binding["voice_model"],
            voice_provider_voice_id=voice_binding["voice_provider_voice_id"],
            voice_source_type=voice_binding["voice_source_type"],
            voice_profile=voice_profile,
            llm_preset_id=llm_binding["llm_preset_id"],
            visibility=character_in.visibility.value,
            creator_id=current_user.id,
        )
        created = await self._repo.create(character)
        return _to_response(
            created,
            selected_voice,
            distinct_user_count=0,
            creator_username=current_user.username,
        )

    async def list_my_characters(
        self, current_user: User, *, skip: int, limit: int
    ) -> list[CharacterResponse]:
        rows = await self._repo.list_by_creator(current_user.id, skip=skip, limit=limit)
        if not rows:
            return []
        characters = [row[0] for row in rows]
        counts = await self._repo.batch_count_distinct_users([c.id for c in characters])
        responses: list[CharacterResponse] = []
        for character, username in rows:
            voice_item = await self._resolve_character_voice_item(character)
            responses.append(
                _to_response(
                    character,
                    voice_item,
                    distinct_user_count=counts.get(character.id, 0),
                    creator_username=username,
                )
            )
        return responses

    async def list_market_characters(self, *, skip: int, limit: int) -> list[CharacterResponse]:
        rows = await self._repo.list_market(skip=skip, limit=limit)
        if not rows:
            return []
        characters = [row[0] for row in rows]
        counts = await self._repo.batch_count_distinct_users([c.id for c in characters])
        responses: list[CharacterResponse] = []
        for character, username in rows:
            voice_item = await self._resolve_character_voice_item(character)
            responses.append(
                _to_response(
                    character,
                    voice_item,
                    distinct_user_count=counts.get(character.id, 0),
                    creator_username=username,
                )
            )
        return responses

    async def get_character(
        self,
        character_id: uuid.UUID,
        *,
        current_user: User | None,
    ) -> CharacterResponse:
        character = await self._require_character(character_id)

        voice_item = await self._resolve_character_voice_item(character)
        distinct_user_count = await self._repo.count_distinct_users_by_character(character.id)
        creator_username = await self._repo.get_creator_username(character.creator_id)
        return _to_response(
            character,
            voice_item,
            distinct_user_count=distinct_user_count,
            creator_username=creator_username,
        )

    async def update_character(
        self,
        character_id: uuid.UUID,
        *,
        character_in: CharacterUpdate,
        current_user: User,
    ) -> CharacterResponse:
        character = await self._require_character(character_id)

        self._assert_creator(character, current_user, "modify")

        update_data = character_in.model_dump(exclude_unset=True)
        if "visibility" in update_data and update_data["visibility"] is not None:
            update_data["visibility"] = update_data["visibility"].value
        if "avatar_image_key" in update_data:
            update_data["avatar_image_key"] = _validate_character_avatar_image_key_or_raise(
                str(update_data["avatar_image_key"])
                if update_data["avatar_image_key"] is not None
                else None,
                current_user=current_user,
            )
        if "voice_profile" in update_data:
            # ``None`` clears the shaping back to the provider defaults.
            if update_data["voice_profile"] is None:
                update_data["voice_profile"] = {}
            else:
                update_data["voice_profile"] = normalize_voice_profile(
                    update_data["voice_profile"]
                ).to_dict()
        await self._apply_update_llm_binding_or_raise(
            update_data=update_data,
            current_user=current_user,
        )
        selected_voice: VoiceSelectableItem | None = None
        voice_keys = {
            "voice_provider",
            "voice_model",
            "voice_provider_voice_id",
            "voice_source_type",
        }
        if any(key in update_data for key in voice_keys):
            voice_provider = (
                str(update_data.get("voice_provider", character.voice_provider))
                if update_data.get("voice_provider", character.voice_provider) is not None
                else character.voice_provider
            )
            voice_model = (
                str(update_data.get("voice_model", character.voice_model))
                if update_data.get("voice_model", character.voice_model) is not None
                else character.voice_model
            )
            voice_provider_voice_id = (
                str(update_data.get("voice_provider_voice_id", character.voice_provider_voice_id))
                if update_data.get("voice_provider_voice_id", character.voice_provider_voice_id)
                is not None
                else character.voice_provider_voice_id
            )
            raw_source_type = update_data.get("voice_source_type", character.voice_source_type)
            source_type = (
                raw_source_type.value if hasattr(raw_source_type, "value") else str(raw_source_type)
            )
            voice_binding, selected_voice = await self._resolve_selected_voice_or_raise(
                current_user=current_user,
                voice_provider=voice_provider,
                voice_model=voice_model,
                voice_provider_voice_id=voice_provider_voice_id,
                voice_source_type=source_type,
            )
            update_data.update(voice_binding)

        updated = await self._repo.update(character, update_data)
        if not selected_voice:
            selected_voice = await self._resolve_character_voice_item(updated)
        distinct_user_count = await self._repo.count_distinct_users_by_character(updated.id)
        return _to_response(
            updated,
            selected_voice,
            distinct_user_count=distinct_user_count,
            creator_username=current_user.username,
        )

    async def unpublish_character(
        self,
        character_id: uuid.UUID,
        *,
        current_user: User,
    ) -> CharacterResponse:
        character = await self._require_character(character_id)

        self._assert_creator(character, current_user, "unpublish")

        unpublished = await self._repo.unpublish(character)
        selected_voice = await self._resolve_character_voice_item(unpublished)
        distinct_user_count = await self._repo.count_distinct_users_by_character(unpublished.id)
        return _to_response(
            unpublished,
            selected_voice,
            distinct_user_count=distinct_user_count,
            creator_username=current_user.username,
        )

    async def delete_character(
        self,
        character_id: uuid.UUID,
        *,
        current_user: User,
    ) -> None:
        """Permanently delete a character. Only creator can delete.

        Vector memories are purged across all users after the row is gone;
        a purge failure never blocks deletion because every memory query
        scopes by character_id, so leftover vectors stay unreachable.
        """
        character = await self._require_character(character_id)

        self._assert_creator(character, current_user, "delete")

        await self._repo.delete(character)

        if self._memory_repo is not None:
            try:
                await self._memory_repo.purge_character(character_id=str(character.id))
            except Exception:
                logger.exception(
                    "Memory purge failed after character deletion; character_id=%s",
                    character_id,
                )

    async def publish_character(
        self,
        character_id: uuid.UUID,
        *,
        current_user: User,
    ) -> CharacterResponse:
        character = await self._require_character(character_id)

        self._assert_creator(character, current_user, "publish")

        published = await self._repo.publish(character)
        selected_voice = await self._resolve_character_voice_item(published)
        distinct_user_count = await self._repo.count_distinct_users_by_character(published.id)
        return _to_response(
            published,
            selected_voice,
            distinct_user_count=distinct_user_count,
            creator_username=current_user.username,
        )
