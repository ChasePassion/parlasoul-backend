"""Shared character access checks for API services.

Characters are unlisted-capable: PRIVATE ones stay out of every public
listing but anyone holding the id (a share link embeds it) may view and use
them. Access by id is therefore open to any authenticated user.
"""

from __future__ import annotations

import uuid

from src.core.exceptions import APIError
from src.db.models import Character, User
from src.repositories.character_repository import CharacterRepository


class CharacterAccessService:
    """Checks whether a user can access a character's data."""

    def __init__(self, character_repo: CharacterRepository):
        self._character_repo = character_repo

    async def assert_access(self, *, current_user: User, character_id: str) -> Character:
        try:
            character_uuid = uuid.UUID(character_id)
        except ValueError as exc:
            raise APIError(
                status_code=400,
                code="invalid_param",
                message=(
                    "invalid request: character_id is not a valid UUID; "
                    f"character_id={character_id}"
                ),
            ) from exc

        character = await self._character_repo.get_by_id(character_uuid)
        if character is None:
            raise APIError(
                status_code=404,
                code="character_not_found",
                message=f"resource not found: character does not exist; character_id={character_id}",
            )

        return character
