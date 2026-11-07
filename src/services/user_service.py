"""User service for profile and creator homepage operations."""

from __future__ import annotations

import uuid

from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.db.models import Character, User
from src.repositories.character_repository import CharacterRepository
from src.repositories.user_repository import UserRepository
from src.schemas.character import CharacterResponse
from src.schemas.media import MediaUploadKind
from src.schemas.user import OnboardingStatusUpdate, UserProfileResponse, UserProfileUpdate
from src.services.media_url_service import (
    build_avatar_urls,
    validate_avatar_image_key_owner,
)


def _to_profile_response(user: User) -> UserProfileResponse:
    return UserProfileResponse(
        id=str(user.id),
        email=user.email,
        username=user.username,
        email_verified=user.email_verified,
        avatar_image_key=user.avatar_image_key,
        avatar_urls=build_avatar_urls(user.avatar_image_key),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        onboarding_status=user.onboarding_status,
    )


def _to_character_response(
    character: Character,
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
        llm_preset_id=character.llm_preset_id,
        creator_id=str(character.creator_id) if character.creator_id else None,
        creator_username=creator_username,
        unpublished_at=character.unpublished_at,
        visibility=character.visibility,
        identifier=character.identifier,
        distinct_user_count=distinct_user_count,
    )


class UserService:
    """Business logic for user/profile operations."""

    def __init__(
        self,
        user_repo: UserRepository,
        character_repo: CharacterRepository,
    ):
        self._user_repo = user_repo
        self._character_repo = character_repo

    async def get_my_profile(self, current_user: User) -> UserProfileResponse:
        return _to_profile_response(current_user)

    async def update_my_profile(
        self,
        profile: UserProfileUpdate,
        *,
        current_user: User,
    ) -> UserProfileResponse:
        if profile.username is None and profile.avatar_image_key is None:
            raise APIError(
                status_code=400,
                code="user_profile_update_empty",
                message="invalid request: at least one field is required; fields=username,avatar_image_key",
            )

        avatar_image_key = profile.avatar_image_key
        if avatar_image_key is not None:
            avatar_image_key = validate_avatar_image_key_owner(
                avatar_image_key,
                owner_id=str(current_user.id),
                allowed_kinds=(MediaUploadKind.USER_AVATAR,),
            )
            if avatar_image_key is None:
                raise APIError(
                    status_code=422,
                    code=ErrorCode.VALIDATION_FAILED,
                    message="validation failed: avatar image key is not owned by current user",
                )
        updated = await self._user_repo.update_profile(
            current_user,
            username=profile.username,
            avatar_image_key=avatar_image_key,
        )
        return _to_profile_response(updated)

    async def update_onboarding_status(
        self,
        payload: OnboardingStatusUpdate,
        *,
        current_user: User,
    ) -> UserProfileResponse:
        updated = await self._user_repo.update_onboarding_status(
            current_user,
            status=payload.status,
        )
        return _to_profile_response(updated)

    async def get_creator_characters(
        self,
        *,
        creator_id: uuid.UUID,
        skip: int,
        limit: int,
        current_user: User | None,
    ) -> list[CharacterResponse]:
        is_owner = current_user is not None and current_user.id == creator_id
        characters = await self._user_repo.list_creator_characters(
            creator_id,
            skip=skip,
            limit=limit,
            public_only=not is_owner,
        )
        if not characters:
            return []
        creator = current_user if is_owner else await self._user_repo.get_by_id(creator_id)
        creator_username = creator.username if creator else None
        counts = await self._character_repo.batch_count_distinct_users([c.id for c in characters])
        return [
            _to_character_response(
                character,
                distinct_user_count=counts.get(character.id, 0),
                creator_username=creator_username,
            )
            for character in characters
        ]
