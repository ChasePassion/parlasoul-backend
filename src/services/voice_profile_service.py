"""Business logic for voice profile APIs."""

from __future__ import annotations

import base64
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import cast

from src.core.config import settings
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.core.tts_registry import TTSRegistry
from src.core.tts_registry import tts_registry as default_tts_registry
from src.core.uow import UnitOfWork
from src.db.models import User, VoiceProfile, VoiceProfileStatus, VoiceSourceType
from src.repositories.character_repository import CharacterRepository
from src.repositories.voice_gateway_repository import VoiceGatewayRepository
from src.repositories.voice_profile_repository import VoiceProfileRepository
from src.schemas.media import MediaUploadKind
from src.schemas.voice_profile import (
    VoiceCatalogResponse,
    VoiceCloneAccepted,
    VoiceProfileResponse,
    VoiceProfilesPageResponse,
    VoiceSelectableItem,
    VoiceSourceTypeEnum,
    VoiceStatusEnum,
)
from src.services.media_url_service import (
    build_avatar_urls,
    validate_avatar_image_key_owner,
)
from src.services.subscription_service import SubscriptionService
from src.services.tts_gateway_service import TTSGatewayService

logger = logging.getLogger(__name__)
_PREVIEW_TEXT_MAX_LEN = 120


def _normalize_display_name_or_raise(display_name: str | None) -> str:
    normalized = (display_name or "").strip()
    if not normalized:
        raise APIError(
            status_code=422,
            code="validation_failed",
            message="validation failed: display_name is required",
        )
    if len(normalized) > 40:
        raise APIError(
            status_code=422,
            code="validation_failed",
            message="validation failed: display_name is too long; max_length=40",
        )
    return normalized


def _decode_audio_b64(audio_b64: str) -> bytes:
    payload = audio_b64.strip()
    if not payload:
        return b""
    missing_padding = len(payload) % 4
    if missing_padding:
        payload = payload + ("=" * (4 - missing_padding))
    return base64.b64decode(payload)


async def _next_decoded_audio_chunk(
    stream: AsyncGenerator[tuple[str, str], None],
) -> tuple[bytes, str] | None:
    async for audio_b64, mime_type in stream:
        decoded = _decode_audio_b64(audio_b64)
        if decoded:
            return decoded, mime_type
    return None


def _normalize_preview_text_or_raise(preview_text: str | None) -> str:
    normalized = (preview_text or "").strip()
    if not normalized:
        raise APIError(
            status_code=422,
            code="validation_failed",
            message="validation failed: preview_text is required",
        )
    if len(normalized) > _PREVIEW_TEXT_MAX_LEN:
        raise APIError(
            status_code=422,
            code="validation_failed",
            message=(
                f"validation failed: preview_text is too long; max_length={_PREVIEW_TEXT_MAX_LEN}"
            ),
        )
    return normalized


def _normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_avatar_image_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_voice_avatar_image_key_or_raise(
    image_key: str | None,
    *,
    current_user: User,
) -> str | None:
    normalized_input = _normalize_avatar_image_key(image_key)
    if normalized_input is None:
        return None
    normalized = validate_avatar_image_key_owner(
        normalized_input,
        owner_id=str(current_user.id),
        allowed_kinds=(
            MediaUploadKind.VOICE_AVATAR,
            MediaUploadKind.CHARACTER_AVATAR,
        ),
    )
    if normalized is None:
        raise APIError(
            status_code=422,
            code=ErrorCode.VALIDATION_FAILED,
            message="validation failed: avatar image key is not owned by current user",
        )
    return normalized


def _normalize_character_ids_or_raise(character_ids: object) -> list[uuid.UUID]:
    if character_ids is None:
        return []
    if not isinstance(character_ids, list):
        raise APIError(
            status_code=422,
            code="validation_failed",
            message="validation failed: character_ids must be a list",
        )

    normalized: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw in character_ids:
        if not isinstance(raw, str):
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: character_ids items must be strings",
            )
        try:
            value = uuid.UUID(raw)
        except ValueError as exc:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message=f"validation failed: invalid character_id; value={raw}",
            ) from exc
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _to_selectable_item(profile: VoiceProfile) -> VoiceSelectableItem:
    usage_hint = None
    if profile.meta and isinstance(profile.meta, dict):
        raw = profile.meta.get("usage_hint")
        if isinstance(raw, str):
            usage_hint = raw

    return VoiceSelectableItem(
        id=str(profile.id),
        display_name=profile.display_name,
        source_type=VoiceSourceTypeEnum(profile.source_type),
        provider=profile.provider,
        provider_model=profile.provider_model,
        provider_voice_id=profile.provider_voice_id,
        avatar_image_key=profile.avatar_image_key,
        avatar_urls=build_avatar_urls(profile.avatar_image_key),
        preview_text=profile.preview_text,
        preview_audio_url=profile.preview_audio_url,
        usage_hint=usage_hint,
    )


def _to_response(
    profile: VoiceProfile,
    *,
    bound_character_count: int = 0,
    bound_character_ids: list[str] | None = None,
) -> VoiceProfileResponse:
    return VoiceProfileResponse(
        id=str(profile.id),
        owner_user_id=str(profile.owner_user_id) if profile.owner_user_id else None,
        provider=profile.provider,
        provider_voice_id=profile.provider_voice_id,
        provider_model=profile.provider_model,
        source_type=VoiceSourceTypeEnum(profile.source_type),
        display_name=profile.display_name,
        description=profile.description,
        avatar_image_key=profile.avatar_image_key,
        avatar_urls=build_avatar_urls(profile.avatar_image_key),
        status=VoiceStatusEnum(profile.status),
        provider_status=profile.provider_status,
        preview_text=profile.preview_text,
        preview_audio_url=profile.preview_audio_url,
        language_tags=profile.language_tags,
        metadata=profile.meta,
        bound_character_count=bound_character_count,
        bound_character_ids=bound_character_ids,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _normalize_status(provider_status: str | None) -> str:
    if not provider_status:
        return VoiceProfileStatus.PROCESSING.value
    normalized = provider_status.strip().lower()
    if normalized in {"active", "ready", "available", "completed"}:
        return VoiceProfileStatus.READY.value
    if normalized in {"failed", "error", "rejected"}:
        return VoiceProfileStatus.FAILED.value
    if normalized in {"deleted"}:
        return VoiceProfileStatus.DELETED.value
    if normalized in {"deleting"}:
        return VoiceProfileStatus.DELETING.value
    return VoiceProfileStatus.PROCESSING.value


class VoiceProfileService:
    """Business logic for voice profile CRUD and clone flow."""

    def __init__(
        self,
        repo: VoiceProfileRepository,
        gateway_repo: VoiceGatewayRepository,
        character_repo: CharacterRepository,
        subscription_service: SubscriptionService,
        uow: UnitOfWork,
        *,
        tts_gateway_service: TTSGatewayService | None = None,
        tts_registry: TTSRegistry | None = None,
    ) -> None:
        self._repo: VoiceProfileRepository = repo
        self._gateway_repo: VoiceGatewayRepository = gateway_repo
        self._character_repo: CharacterRepository = character_repo
        self._subscription_service: SubscriptionService = subscription_service
        self._uow: UnitOfWork = uow
        self._tts_registry = tts_registry or default_tts_registry
        self._tts_gateway_service = tts_gateway_service or TTSGatewayService(
            registry=self._tts_registry,
            dashscope_gateway=gateway_repo,
        )

    @staticmethod
    def _should_skip_upstream_delete(profile: VoiceProfile) -> bool:
        provider_voice_id = profile.provider_voice_id.strip()
        # Local placeholder IDs mean clone creation never produced a real upstream voice.
        return provider_voice_id.startswith("pending-")

    async def _list_bound_character_ids(
        self,
        *,
        profile: VoiceProfile,
        current_user: User,
    ) -> list[uuid.UUID]:
        return await self._character_repo.list_ids_by_voice_binding(
            creator_id=current_user.id,
            voice_provider=profile.provider,
            voice_provider_voice_id=profile.provider_voice_id,
            voice_source_type=profile.source_type,
        )

    async def _list_bound_character_counts(
        self,
        *,
        profiles: list[VoiceProfile],
        current_user: User,
    ) -> dict[tuple[str, str, str], int]:
        bindings = [
            (profile.provider, profile.provider_voice_id, profile.source_type)
            for profile in profiles
        ]
        return await self._character_repo.list_bound_character_counts(
            creator_id=current_user.id,
            bindings=bindings,
        )

    async def _ensure_owned_characters_or_raise(
        self,
        *,
        current_user: User,
        character_ids: list[uuid.UUID],
    ) -> None:
        if not character_ids:
            return

        characters = await self._character_repo.get_by_ids_for_creator(
            creator_id=current_user.id,
            character_ids=character_ids,
        )
        if len(characters) != len(character_ids):
            raise APIError(
                status_code=404,
                code="character_not_found",
                message="resource not found: one or more characters do not exist for current user",
            )

    @staticmethod
    def _require_profile_model_or_raise(profile: VoiceProfile) -> str:
        provider_model = (profile.provider_model or "").strip()
        if provider_model:
            return provider_model
        raise APIError(
            status_code=409,
            code="voice_profile_model_missing",
            message=(
                f"resource conflict: voice profile provider_model is missing; voice_id={profile.id}"
            ),
        )

    async def _sync_bound_characters(
        self,
        *,
        profile: VoiceProfile,
        current_user: User,
        requested_character_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        current_character_ids = await self._list_bound_character_ids(
            profile=profile,
            current_user=current_user,
        )
        current_set = set(current_character_ids)
        requested_set = set(requested_character_ids)

        to_assign = [
            character_id
            for character_id in requested_character_ids
            if character_id not in current_set
        ]
        to_reset = [
            character_id
            for character_id in current_character_ids
            if character_id not in requested_set
        ]

        if to_assign:
            await self._character_repo.assign_voice_to_characters(
                creator_id=current_user.id,
                character_ids=to_assign,
                voice_provider=profile.provider,
                voice_model=self._require_profile_model_or_raise(profile),
                voice_provider_voice_id=profile.provider_voice_id,
                voice_source_type=profile.source_type,
            )
        if to_reset:
            await self._character_repo.reset_voice_to_default_for_characters(
                creator_id=current_user.id,
                character_ids=to_reset,
            )

        return requested_character_ids

    async def list_my_voices(
        self,
        *,
        current_user: User,
        status: str | None,
        source_type: str | None,
        cursor: str | None,
        limit: int,
    ) -> VoiceProfilesPageResponse:
        items, next_cursor, has_more = await self._repo.list_by_owner(
            owner_user_id=current_user.id,
            status=status,
            source_type=source_type,
            cursor=cursor,
            limit=limit,
        )
        counts = await self._list_bound_character_counts(profiles=items, current_user=current_user)
        return VoiceProfilesPageResponse(
            items=[
                _to_response(
                    item,
                    bound_character_count=counts.get(
                        (item.provider, item.provider_voice_id, item.source_type),
                        0,
                    ),
                )
                for item in items
            ],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def list_catalog(
        self,
        *,
        current_user: User,
        provider: str | None,
        model: str | None,
        include_system: bool,
        include_user_custom: bool,
    ) -> VoiceCatalogResponse:
        provider_filter = provider.strip().lower() if provider and provider.strip() else None
        items: list[VoiceSelectableItem] = []

        if include_system:
            system_provider = provider_filter or self._tts_registry.router.system_provider
            system_route = self._tts_registry.resolve_system_route(
                provider=system_provider,
                model=model,
            )
            system_voices = self._tts_registry.list_system_voices(
                provider=system_route.provider,
                model=system_route.model,
            )
            for item in system_voices:
                provider_voice_id = item.get("provider_voice_id")
                display_name = item.get("display_name")
                if not isinstance(provider_voice_id, str) or not provider_voice_id.strip():
                    continue
                if not isinstance(display_name, str) or not display_name.strip():
                    display_name = provider_voice_id
                avatar_key = item.get("avatar_image_key") if isinstance(item.get("avatar_image_key"), str) else None
                item_provider = item.get("provider") or system_route.provider
                item_model = item.get("provider_model") or system_route.model
                items.append(
                    VoiceSelectableItem(
                        id=f"sys:{item_provider}:{item_model}:{provider_voice_id}",
                        display_name=display_name,
                        source_type=VoiceSourceTypeEnum.SYSTEM,
                        provider=item_provider,
                        provider_model=item_model,
                        provider_voice_id=provider_voice_id,
                        avatar_image_key=avatar_key,
                        avatar_urls=build_avatar_urls(avatar_key),
                        preview_audio_url=item.get("preview_audio_url"),
                        usage_hint=(
                            item.get("usage_hint")
                            if isinstance(item.get("usage_hint"), str)
                            else None
                        ),
                    )
                )

        if include_user_custom:
            # Clone voices carry their own provider_model and remain selectable even when the
            # current character is still bound to a non-clone model.
            custom_items = await self._repo.list_user_ready_custom(
                user_id=current_user.id,
                provider=provider_filter,
                provider_model=None,
            )
            items.extend(_to_selectable_item(item) for item in custom_items)

        return VoiceCatalogResponse(items=items)

    async def create_clone(
        self,
        *,
        current_user: User,
        provider: str,
        display_name: str,
        preview_text: str,
        source_audio_bytes: bytes,
        source_audio_format: str,
        description: str | None,
        avatar_image_key: str | None = None,
        language_hint: str | None,
        idempotency_key: str | None,
    ) -> VoiceCloneAccepted:
        await self._subscription_service.assert_feature(current_user, "voice_clone")

        if provider.strip().lower() != "dashscope":
            raise APIError(
                status_code=400,
                code="voice_provider_unsupported",
                message=f"invalid request: unsupported provider; provider={provider}",
            )
        if not source_audio_bytes:
            raise APIError(
                status_code=422,
                code="voice_clone_audio_empty",
                message="invalid request: source audio payload is empty",
            )
        if len(source_audio_bytes) > settings.UPLOAD_MAX_FILE_SIZE_BYTES * 2:
            raise APIError(
                status_code=413,
                code="voice_clone_audio_too_large",
                message=(
                    "payload too large: source audio exceeds size limit; "
                    f"limit_bytes={settings.UPLOAD_MAX_FILE_SIZE_BYTES * 2}"
                ),
            )
        normalized_display_name = _normalize_display_name_or_raise(display_name)
        normalized_preview_text = _normalize_preview_text_or_raise(preview_text)

        try:
            clone_result = await self._gateway_repo.create_voice_clone(
                provider="dashscope",
                display_name=normalized_display_name,
                source_audio_bytes=source_audio_bytes,
                source_audio_format=source_audio_format,
                description=_normalize_description(description),
                language_hint=language_hint,
                idempotency_key=idempotency_key,
            )
        except APIError:
            raise

        provider_status = clone_result.get("provider_status")
        provider_model = clone_result.get("provider_model")
        if not isinstance(provider_model, str) or not provider_model.strip():
            raise APIError(
                status_code=502,
                code="voice_provider_invalid_response",
                message="upstream error: create clone response missing provider_model",
            )

        profile = VoiceProfile(
            owner_user_id=current_user.id,
            provider="dashscope",
            provider_voice_id=clone_result["provider_voice_id"],
            source_type=VoiceSourceType.CLONE.value,
            status=_normalize_status(provider_status),
            provider_status=provider_status,
            provider_model=provider_model.strip(),
            display_name=normalized_display_name,
            description=_normalize_description(description),
            avatar_image_key=_validate_voice_avatar_image_key_or_raise(
                avatar_image_key,
                current_user=current_user,
            ),
            preview_text=normalized_preview_text,
            meta={
                "audio_format": source_audio_format,
                "language_hint": language_hint,
                "idempotency_key": idempotency_key,
            },
        )
        if clone_result.get("preview_audio_url"):
            profile.preview_audio_url = cast(str, clone_result["preview_audio_url"])
        async with self._uow.transactional():
            profile = await self._repo.create(profile)

        return VoiceCloneAccepted(
            voice=_to_response(profile, bound_character_count=0, bound_character_ids=[]),
            provider_request_id=clone_result.get("provider_request_id"),
            estimated_ready_seconds=clone_result.get("estimated_ready_seconds"),
        )

    async def get_voice_by_id(
        self,
        *,
        voice_id: uuid.UUID,
        current_user: User,
    ) -> VoiceProfileResponse:
        profile = await self._repo.get_by_id_for_owner(
            voice_id=voice_id, owner_user_id=current_user.id
        )
        if not profile:
            raise APIError(
                status_code=404,
                code="voice_profile_not_found",
                message=f"resource not found: voice profile does not exist; voice_id={voice_id}",
            )
        bound_character_ids = await self._list_bound_character_ids(
            profile=profile, current_user=current_user
        )
        return _to_response(
            profile,
            bound_character_count=len(bound_character_ids),
            bound_character_ids=[str(character_id) for character_id in bound_character_ids],
        )

    async def patch_voice_by_id(
        self,
        *,
        voice_id: uuid.UUID,
        current_user: User,
        patch_data: dict[str, object],
    ) -> VoiceProfileResponse:
        profile = await self._repo.get_by_id_for_owner(
            voice_id=voice_id, owner_user_id=current_user.id
        )
        if not profile:
            raise APIError(
                status_code=404,
                code="voice_profile_not_found",
                message=f"resource not found: voice profile does not exist; voice_id={voice_id}",
            )

        update_data: dict[str, object] = {}
        requested_character_ids: list[uuid.UUID] | None = None

        if "display_name" in patch_data:
            update_data["display_name"] = _normalize_display_name_or_raise(
                str(patch_data.get("display_name"))
                if patch_data.get("display_name") is not None
                else None
            )
        if "description" in patch_data:
            update_data["description"] = _normalize_description(
                str(patch_data.get("description"))
                if patch_data.get("description") is not None
                else None
            )
        if "preview_text" in patch_data:
            update_data["preview_text"] = _normalize_preview_text_or_raise(
                str(patch_data.get("preview_text"))
                if patch_data.get("preview_text") is not None
                else None
            )
        if "avatar_image_key" in patch_data:
            update_data["avatar_image_key"] = _validate_voice_avatar_image_key_or_raise(
                str(patch_data.get("avatar_image_key"))
                if patch_data.get("avatar_image_key") is not None
                else None,
                current_user=current_user,
            )
        if "character_ids" in patch_data:
            requested_character_ids = _normalize_character_ids_or_raise(
                patch_data.get("character_ids")
            )
            await self._ensure_owned_characters_or_raise(
                current_user=current_user,
                character_ids=requested_character_ids,
            )

        if not update_data and requested_character_ids is None:
            raise APIError(
                status_code=400,
                code="voice_profile_patch_empty",
                message=(
                    "invalid request: at least one field is required; "
                    "fields=display_name,description,preview_text,avatar_image_key,character_ids"
                ),
            )

        updated = profile
        async with self._uow.transactional():
            if update_data:
                updated = await self._repo.update(profile, update_data)
            if requested_character_ids is not None:
                await self._sync_bound_characters(
                    profile=updated,
                    current_user=current_user,
                    requested_character_ids=requested_character_ids,
                )

        bound_character_ids = await self._list_bound_character_ids(
            profile=updated, current_user=current_user
        )
        return _to_response(
            updated,
            bound_character_count=len(bound_character_ids),
            bound_character_ids=[str(character_id) for character_id in bound_character_ids],
        )

    async def get_preview_audio_stream(
        self,
        *,
        voice_id: uuid.UUID,
        current_user: User,
        audio_format: str,
    ) -> tuple[str, AsyncGenerator[bytes, None]]:
        profile = await self._repo.get_by_id_for_owner(
            voice_id=voice_id, owner_user_id=current_user.id
        )
        if not profile:
            raise APIError(
                status_code=404,
                code="voice_profile_not_found",
                message=f"resource not found: voice profile does not exist; voice_id={voice_id}",
            )
        if profile.source_type != VoiceSourceType.CLONE.value:
            raise APIError(
                status_code=400,
                code="voice_preview_not_supported",
                message=(
                    "invalid request: preview streaming is only supported for clone voices; "
                    f"voice_id={voice_id}; source_type={profile.source_type}"
                ),
            )
        if profile.status != VoiceProfileStatus.READY.value:
            raise APIError(
                status_code=409,
                code="voice_profile_not_ready",
                message=(
                    "resource conflict: voice profile is not ready for preview; "
                    f"voice_id={voice_id}; status={profile.status}"
                ),
            )

        preview_text = (profile.preview_text or "").strip()
        if not preview_text:
            raise APIError(
                status_code=409,
                code="voice_preview_text_missing",
                message=(
                    f"resource conflict: voice profile preview text is missing; voice_id={voice_id}"
                ),
            )

        source_stream = self._tts_gateway_service.stream_tts_audio_base64(
            text=preview_text,
            audio_format=audio_format,
            instructions=settings.DASHSCOPE_TTS_INSTRUCTIONS_DEFAULT or None,
            provider=profile.provider,
            provider_voice_id=profile.provider_voice_id,
            provider_model=profile.provider_model,
            voice_source_type=profile.source_type,
        )
        first_chunk = await _next_decoded_audio_chunk(source_stream)
        if first_chunk is None:
            raise APIError(
                status_code=502,
                code="tts_upstream_error",
                message="upstream error: tts service produced empty audio stream",
            )

        first_chunk_bytes, mime_type = first_chunk

        async def _audio_generator() -> AsyncGenerator[bytes, None]:
            yield first_chunk_bytes
            try:
                while True:
                    next_chunk = await _next_decoded_audio_chunk(source_stream)
                    if next_chunk is None:
                        break
                    chunk, _ = next_chunk
                    yield chunk
            except APIError as exc:
                logger.warning(
                    "Voice preview stream interrupted after first chunk: %s", exc.message
                )
            except Exception as exc:
                logger.exception("Unexpected voice preview interruption: %s", exc)

        return mime_type, _audio_generator()

    async def delete_voice_by_id(
        self,
        *,
        voice_id: uuid.UUID,
        current_user: User,
    ) -> None:
        profile = await self._repo.get_by_id_for_owner(
            voice_id=voice_id, owner_user_id=current_user.id
        )
        if not profile:
            raise APIError(
                status_code=404,
                code="voice_profile_not_found",
                message=f"resource not found: voice profile does not exist; voice_id={voice_id}",
            )

        refs = await self._repo.count_referenced_characters(
            provider=profile.provider,
            provider_voice_id=profile.provider_voice_id,
        )
        if refs > 0:
            raise APIError(
                status_code=409,
                code="voice_profile_in_use",
                message=(
                    "resource conflict: voice profile is referenced by characters; "
                    f"voice_id={voice_id}; refs={refs}"
                ),
            )

        if not self._should_skip_upstream_delete(profile):
            try:
                _ = await self._gateway_repo.delete_voice_clone(
                    provider=profile.provider,
                    provider_voice_id=profile.provider_voice_id,
                )
            except APIError as exc:
                if exc.status_code == 404:
                    logger.warning(
                        "Upstream voice already deleted, continue local delete; voice_id=%s; provider=%s; provider_voice_id=%s",
                        profile.id,
                        profile.provider,
                        profile.provider_voice_id,
                    )
                else:
                    raise

        async with self._uow.transactional():
            await self._repo.delete(profile)
