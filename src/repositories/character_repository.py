"""Character repository for database access."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select, text, update
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.tts_registry import (
    DEFAULT_SYSTEM_TTS_MODEL,
    DEFAULT_SYSTEM_TTS_PROVIDER,
    DEFAULT_SYSTEM_TTS_VOICE_ID,
)
from src.db.models import (
    AuthorType,
    Character,
    Chat,
    Turn,
    User,
    VisibilityType,
)


class CharacterRepository:
    """Data access methods for characters."""

    def __init__(self, db: AsyncSession):
        self._db = db

    @asynccontextmanager
    async def _system_aggregate_scope(self) -> AsyncIterator[None]:
        """Read cross-user aggregates without weakening the request RLS scope.

        Character cards expose only a public distinct-user count, but the
        user-scoped RLS policies on chats/turns hide every row from a public or
        unrelated request. Switch to the system execution context for the
        aggregate query, then restore the caller's context before returning.
        """
        previous = (
            await self._db.execute(
                text("SELECT current_setting('app.current_user_id', true)")
            )
        ).scalar_one_or_none() or ""
        await self._db.execute(
            text("SELECT set_config('app.current_user_id', 'system', true)")
        )
        try:
            yield
        finally:
            await self._db.execute(
                text("SELECT set_config('app.current_user_id', :user_id, true)"),
                {"user_id": previous},
            )

    async def create(self, character: Character) -> Character:
        self._db.add(character)
        await self._db.commit()
        await self._db.refresh(character)
        return character

    async def list_by_creator(
        self,
        creator_id: uuid.UUID,
        *,
        skip: int,
        limit: int,
    ) -> list[tuple[Character, str | None]]:
        stmt = (
            select(Character, User.username)
            .outerjoin(User, Character.creator_id == User.id)
            .where(
                Character.creator_id == creator_id,
                Character.deleted_at.is_(None),
            )
            .offset(skip)
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def list_market(self, *, skip: int, limit: int) -> list[tuple[Character, str | None]]:
        stmt = (
            select(Character, User.username)
            .outerjoin(User, Character.creator_id == User.id)
            .where(
                Character.visibility == VisibilityType.PUBLIC.value,
                Character.deleted_at.is_(None),
            )
            .order_by(Character.created_at.asc(), Character.id.asc())
            .offset(skip)
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def get_by_id(self, character_id: uuid.UUID) -> Character | None:
        return await self._db.get(Character, character_id)

    async def get_creator_username(self, creator_id: uuid.UUID | None) -> str | None:
        if creator_id is None:
            return None
        user = await self._db.get(User, creator_id)
        return user.username if user else None

    async def get_by_ids_for_creator(
        self,
        *,
        creator_id: uuid.UUID,
        character_ids: list[uuid.UUID],
    ) -> list[Character]:
        if not character_ids:
            return []

        stmt = select(Character).where(
            Character.creator_id == creator_id,
            Character.id.in_(character_ids),
            Character.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_ids_by_voice_binding(
        self,
        *,
        creator_id: uuid.UUID,
        voice_provider: str,
        voice_provider_voice_id: str,
        voice_source_type: str,
    ) -> list[uuid.UUID]:
        stmt = select(Character.id).where(
            Character.creator_id == creator_id,
            Character.voice_provider == voice_provider,
            Character.voice_provider_voice_id == voice_provider_voice_id,
            Character.voice_source_type == voice_source_type,
            Character.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        return [row[0] for row in result.all()]

    async def list_bound_character_counts(
        self,
        *,
        creator_id: uuid.UUID,
        bindings: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], int]:
        if not bindings:
            return {}

        predicates = [
            and_(
                Character.voice_provider == voice_provider,
                Character.voice_provider_voice_id == voice_provider_voice_id,
                Character.voice_source_type == voice_source_type,
            )
            for voice_provider, voice_provider_voice_id, voice_source_type in bindings
        ]
        stmt = (
            select(
                Character.voice_provider,
                Character.voice_provider_voice_id,
                Character.voice_source_type,
                func.count(Character.id),
            )
            .where(
                Character.creator_id == creator_id,
                Character.deleted_at.is_(None),
                or_(*predicates),
            )
            .group_by(
                Character.voice_provider,
                Character.voice_provider_voice_id,
                Character.voice_source_type,
            )
        )
        result = await self._db.execute(stmt)
        return {
            (voice_provider, voice_provider_voice_id, voice_source_type): int(count or 0)
            for voice_provider, voice_provider_voice_id, voice_source_type, count in result.all()
        }

    async def assign_voice_to_characters(
        self,
        *,
        creator_id: uuid.UUID,
        character_ids: list[uuid.UUID],
        voice_provider: str,
        voice_model: str,
        voice_provider_voice_id: str,
        voice_source_type: str,
    ) -> None:
        if not character_ids:
            return

        stmt = (
            update(Character)
            .where(
                Character.creator_id == creator_id,
                Character.id.in_(character_ids),
            )
            .values(
                voice_provider=voice_provider,
                voice_model=voice_model,
                voice_provider_voice_id=voice_provider_voice_id,
                voice_source_type=voice_source_type,
            )
        )
        await self._db.execute(stmt)
        await self._db.flush()

    async def reset_voice_to_default_for_characters(
        self,
        *,
        creator_id: uuid.UUID,
        character_ids: list[uuid.UUID],
    ) -> None:
        if not character_ids:
            return

        stmt = (
            update(Character)
            .where(
                Character.creator_id == creator_id,
                Character.id.in_(character_ids),
            )
            .values(
                voice_provider=DEFAULT_SYSTEM_TTS_PROVIDER,
                voice_model=DEFAULT_SYSTEM_TTS_MODEL,
                voice_provider_voice_id=DEFAULT_SYSTEM_TTS_VOICE_ID,
                voice_source_type="system",
            )
        )
        await self._db.execute(stmt)
        await self._db.flush()

    async def update(self, character: Character, update_data: dict[str, object]) -> Character:
        for field, value in update_data.items():
            setattr(character, field, value)
        await self._db.commit()
        await self._db.refresh(character)
        return character

    async def unpublish(self, character: Character) -> Character:
        """Withdraw a character from the public market by making it private."""
        if character.visibility != VisibilityType.PRIVATE.value:
            character.visibility = VisibilityType.PRIVATE.value
            character.unpublished_at = datetime.now(UTC)
            await self._db.commit()
            await self._db.refresh(character)
        return character

    async def soft_delete(self, character: Character) -> None:
        """Hide the character while keeping its chats and statistics."""
        now = datetime.now(UTC)
        character.deleted_at = now
        character.visibility = VisibilityType.PRIVATE.value
        character.unpublished_at = now
        await self._db.commit()

    async def publish(self, character: Character) -> Character:
        """Put a character back on the public market."""
        character.visibility = VisibilityType.PUBLIC.value
        character.unpublished_at = None
        await self._db.commit()
        await self._db.refresh(character)
        return character

    async def count_distinct_users_by_character(self, character_id: uuid.UUID) -> int:
        """统计曾向该角色发送过消息的独立用户数。"""
        stmt = (
            select(func.count(func.distinct(Turn.author_user_id)))
            .join(Chat, Chat.id == Turn.chat_id)
            .where(
                Chat.character_id == character_id,
                Turn.author_type == AuthorType.USER.value,
                Turn.author_user_id.isnot(None),
            )
        )
        async with self._system_aggregate_scope():
            result = await self._db.execute(stmt)
            value = result.scalar_one()
        return int(value or 0)

    async def batch_count_distinct_users(
        self,
        character_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        """批量查询多个角色的独立用户数。"""
        if not character_ids:
            return {}
        stmt = (
            select(
                Chat.character_id,
                func.count(func.distinct(Turn.author_user_id)).label("cnt"),
            )
            .join(Turn, Turn.chat_id == Chat.id)
            .where(
                Chat.character_id.in_(character_ids),
                Turn.author_type == AuthorType.USER.value,
                Turn.author_user_id.isnot(None),
            )
            .group_by(Chat.character_id)
        )
        async with self._system_aggregate_scope():
            result = await self._db.execute(stmt)
            rows = result.all()
        return {row[0]: int(row[1] or 0) for row in rows}
