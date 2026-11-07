"""Service layer for proactive character preference APIs."""

from __future__ import annotations

import uuid

from src.core.exceptions import APIError
from src.core.uow import UnitOfWork
from src.db.models import Character, User
from src.repositories.proactive_character_preference_repository import (
    ProactiveCharacterPreferenceRepository,
)
from src.schemas.chat import CharacterBrief
from src.schemas.proactive import (
    ProactiveCharacterPreferenceItem,
    ProactiveCharacterPreferenceListResponse,
)
from src.services.media_url_service import build_avatar_urls


def _to_character_brief(character: Character, *, creator_username: str | None) -> CharacterBrief:
    return CharacterBrief(
        id=str(character.id),
        name=character.name,
        description=character.description,
        greeting_message=character.greeting_message,
        avatar_image_key=character.avatar_image_key,
        avatar_urls=build_avatar_urls(character.avatar_image_key),
        unpublished_at=getattr(character, "unpublished_at", None),
        visibility=character.visibility,
        creator_id=str(character.creator_id) if character.creator_id else None,
        creator_username=creator_username,
    )


class ProactivePreferenceService:
    """Business logic for proactive character preference management."""

    def __init__(
        self,
        preference_repo: ProactiveCharacterPreferenceRepository,
        uow: UnitOfWork,
    ) -> None:
        self._preference_repo = preference_repo
        self._uow = uow

    async def list_my_preferences(
        self,
        *,
        current_user: User,
    ) -> ProactiveCharacterPreferenceListResponse:
        async with self._uow.transactional():
            rows = await self._preference_repo.list_items_for_user(current_user.id)
        return ProactiveCharacterPreferenceListResponse(
            items=[
                ProactiveCharacterPreferenceItem(
                    character=_to_character_brief(character, creator_username=creator_username),
                    enabled=preference.enabled,
                    last_interaction_at=last_interaction_at,
                )
                for preference, character, creator_username, last_interaction_at in rows
            ]
        )

    async def replace_enabled_character_ids(
        self,
        *,
        current_user: User,
        character_ids: list[str],
    ) -> ProactiveCharacterPreferenceListResponse:
        try:
            requested_ids = {uuid.UUID(value) for value in character_ids}
        except ValueError as exc:
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: character_ids contains invalid uuid",
            ) from exc

        async with self._uow.transactional():
            interacted_ids = set(
                await self._preference_repo.sync_defaults_for_interacted_characters(current_user.id)
            )
            invalid_ids = requested_ids - interacted_ids
            if invalid_ids:
                raise APIError(
                    status_code=400,
                    code="invalid_param",
                    message="invalid request: character_ids contains characters without prior interaction",
                )

            await self._preference_repo.replace_enabled_character_ids(
                user_id=current_user.id,
                character_ids=requested_ids,
            )
            rows = await self._preference_repo.list_items_for_user(current_user.id)

        return ProactiveCharacterPreferenceListResponse(
            items=[
                ProactiveCharacterPreferenceItem(
                    character=_to_character_brief(character, creator_username=creator_username),
                    enabled=preference.enabled,
                    last_interaction_at=last_interaction_at,
                )
                for preference, character, creator_username, last_interaction_at in rows
            ]
        )
