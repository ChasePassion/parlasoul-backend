"""Data access for per-user proactive character preferences."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import (
    AuthorType,
    Character,
    Chat,
    ProactiveCharacterPreference,
    Turn,
    User,
    VisibilityType,
)


class ProactiveCharacterPreferenceRepository:
    """Read/write helpers for proactive character preferences."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def list_interacted_character_ids(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(Chat.character_id)
            .join(Turn, Turn.chat_id == Chat.id)
            .join(Character, Character.id == Chat.character_id)
            .where(
                Chat.user_id == user_id,
                Turn.author_type == AuthorType.USER.value,
                Character.visibility == VisibilityType.PUBLIC.value,
                Character.deleted_at.is_(None),
            )
            .distinct()
        )
        rows = await self._db.execute(stmt)
        return [row[0] for row in rows.all()]

    async def sync_defaults_for_interacted_characters(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        character_ids = await self.list_interacted_character_ids(user_id)
        if not character_ids:
            return []

        stmt = pg_insert(ProactiveCharacterPreference).values(
            [
                {
                    "user_id": user_id,
                    "character_id": character_id,
                    "enabled": True,
                }
                for character_id in character_ids
            ]
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "character_id"])
        await self._db.execute(stmt)
        return character_ids

    async def list_items_for_user(
        self,
        user_id: uuid.UUID,
    ) -> list[tuple[ProactiveCharacterPreference, Character, str | None, datetime | None]]:
        await self.sync_defaults_for_interacted_characters(user_id)

        last_interaction_at = func.max(func.coalesce(Chat.last_turn_at, Chat.created_at)).label(
            "last_interaction_at"
        )
        interacted_subq = (
            select(
                Chat.character_id.label("character_id"),
                last_interaction_at,
            )
            .join(Turn, Turn.chat_id == Chat.id)
            .where(
                Chat.user_id == user_id,
                Turn.author_type == AuthorType.USER.value,
            )
            .group_by(Chat.character_id)
            .subquery()
        )

        stmt = (
            select(
                ProactiveCharacterPreference,
                Character,
                User.username,
                interacted_subq.c.last_interaction_at,
            )
            .join(
                interacted_subq,
                interacted_subq.c.character_id == ProactiveCharacterPreference.character_id,
            )
            .join(Character, Character.id == ProactiveCharacterPreference.character_id)
            .outerjoin(User, User.id == Character.creator_id)
            .where(
                ProactiveCharacterPreference.user_id == user_id,
                Character.visibility == VisibilityType.PUBLIC.value,
                Character.deleted_at.is_(None),
            )
            .order_by(desc(interacted_subq.c.last_interaction_at), desc(Character.id))
        )
        result = await self._db.execute(stmt)
        return list(result.all())

    async def list_enabled_character_ids_for_interacted_user(
        self,
        user_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        await self.sync_defaults_for_interacted_characters(user_id)

        stmt = (
            select(ProactiveCharacterPreference.character_id)
            .join(Character, Character.id == ProactiveCharacterPreference.character_id)
            .where(
                ProactiveCharacterPreference.user_id == user_id,
                ProactiveCharacterPreference.enabled.is_(True),
                Character.visibility == VisibilityType.PUBLIC.value,
                Character.deleted_at.is_(None),
            )
        )
        rows = await self._db.execute(stmt)
        return [row[0] for row in rows.all()]

    async def replace_enabled_character_ids(
        self,
        *,
        user_id: uuid.UUID,
        character_ids: set[uuid.UUID],
    ) -> None:
        interacted_ids = set(await self.sync_defaults_for_interacted_characters(user_id))
        if interacted_ids:
            await self._db.execute(
                update(ProactiveCharacterPreference)
                .where(
                    ProactiveCharacterPreference.user_id == user_id,
                    ProactiveCharacterPreference.character_id.in_(interacted_ids),
                )
                .values(enabled=False)
            )

        if character_ids:
            await self._db.execute(
                update(ProactiveCharacterPreference)
                .where(
                    ProactiveCharacterPreference.user_id == user_id,
                    ProactiveCharacterPreference.character_id.in_(character_ids),
                )
                .values(enabled=True)
            )
