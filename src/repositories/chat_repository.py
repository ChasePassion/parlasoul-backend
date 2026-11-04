"""Chat repository for chat-level data access."""

from __future__ import annotations

import uuid

from datetime import datetime

from sqlalchemy import and_, delete, desc, func, or_, select, text, update
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import AuthorType, Character, Chat, ChatOriginType, ChatState, Turn, User


def _chat_sort_expr():
    return func.coalesce(Chat.last_turn_at, Chat.created_at)


class ChatRepository:
    """Data access methods for chat entities."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_by_id(self, chat_id: uuid.UUID) -> Chat | None:
        return await self._db.get(Chat, chat_id)

    async def get_for_update(self, chat_id: uuid.UUID) -> Chat | None:
        stmt = select(Chat).where(Chat.id == chat_id).with_for_update()
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def get_recent_active(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
    ) -> Chat | None:
        sort_expr = _chat_sort_expr()
        stmt = (
            select(Chat)
            .where(
                Chat.user_id == user_id,
                Chat.character_id == character_id,
                Chat.state == ChatState.ACTIVE.value,
            )
            .order_by(desc(sort_expr), desc(Chat.id))
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def list_active_by_user_and_character(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        cursor_sort_at: datetime | None,
        cursor_chat_id: uuid.UUID | None,
        limit: int,
    ) -> tuple[list[tuple[Chat, Character]], bool]:
        sort_expr = _chat_sort_expr()
        stmt = (
            select(Chat, Character)
            .join(Character, Character.id == Chat.character_id)
            .where(
                Chat.user_id == user_id,
                Chat.character_id == character_id,
                Chat.state == ChatState.ACTIVE.value,
            )
        )

        if cursor_sort_at is not None and cursor_chat_id is not None:
            stmt = stmt.where(
                or_(
                    sort_expr < cursor_sort_at,
                    and_(
                        sort_expr == cursor_sort_at,
                        Chat.id < cursor_chat_id,
                    ),
                )
            )

        stmt = stmt.order_by(desc(sort_expr), desc(Chat.id)).limit(limit + 1)
        rows = list((await self._db.execute(stmt)).all())
        has_more = len(rows) > limit
        items = rows[:limit]
        return [(row[0], row[1]) for row in items], has_more

    async def delete_empty_drafts(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        default_title: str,
    ) -> int:
        has_user_turn = (
            select(Turn.id)
            .where(
                Turn.chat_id == Chat.id,
                Turn.author_type == AuthorType.USER.value,
            )
            .exists()
        )
        has_non_proactive_char_turn = (
            select(Turn.id)
            .where(
                Turn.chat_id == Chat.id,
                Turn.author_type == AuthorType.CHARACTER.value,
                Turn.is_proactive == False,  # noqa: E712
            )
            .exists()
        )
        stmt = delete(Chat).where(
            Chat.user_id == user_id,
            Chat.character_id == character_id,
            Chat.state == ChatState.ACTIVE.value,
            Chat.origin == ChatOriginType.USER.value,
            Chat.title == default_title,
            ~has_user_turn,
            ~has_non_proactive_char_turn,
        )
        result = await self._db.execute(stmt)
        return int(result.rowcount or 0)

    async def update_title_if_first_user_turn(
        self,
        *,
        chat_id: uuid.UUID,
        title: str,
        default_title: str,
    ) -> Chat | None:
        user_turn_count = (
            select(func.count(Turn.id))
            .where(
                Turn.chat_id == Chat.id,
                Turn.author_type == AuthorType.USER.value,
            )
            .scalar_subquery()
        )
        stmt = (
            update(Chat)
            .where(
                Chat.id == chat_id,
                Chat.title == default_title,
                user_turn_count == 1,
            )
            .values(title=title, updated_at=func.now())
            .returning(Chat)
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def list_active_user_character_pairs(self) -> list[tuple[str, str]]:
        stmt = (
            select(Chat.user_id, Chat.character_id)
            .where(Chat.state == ChatState.ACTIVE.value)
            .distinct()
        )
        rows = list((await self._db.execute(stmt)).all())
        return [(str(row.user_id), str(row.character_id)) for row in rows]

    async def try_advisory_lock(self, *, key: int) -> bool:
        bind = self._db.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            return True
        result = await self._db.execute(
            text("SELECT pg_try_advisory_lock(:key) AS locked"),
            {"key": key},
        )
        return bool(result.scalar_one())

    async def advisory_unlock(self, *, key: int) -> None:
        bind = self._db.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            return
        await self._db.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": key},
        )

    def add(self, chat: Chat) -> None:
        self._db.add(chat)

    async def delete(self, chat: Chat) -> None:
        stmt = delete(Chat).where(Chat.id == chat.id)
        await self._db.execute(stmt)

    async def list_characters_with_chats(
        self,
        user_id: uuid.UUID,
        *,
        skip: int,
        limit: int,
    ) -> list[tuple[Character, str | None, datetime | None, bool]]:
        has_interacted = (
            select(Turn.id)
            .where(
                Turn.chat_id == Chat.id,
                or_(
                    Turn.author_type == AuthorType.USER.value,
                    and_(
                        Turn.author_type == AuthorType.CHARACTER.value,
                        Turn.is_proactive == False,  # noqa: E712
                    ),
                ),
            )
            .exists()
        )
        has_active_proactive_chat = (
            select(Turn.id)
            .where(
                Turn.chat_id == Chat.id,
                Chat.origin == ChatOriginType.PROACTIVE.value,
            )
            .exists()
        )
        sort_at = func.max(func.coalesce(Chat.last_turn_at, Chat.created_at)).label("sort_at")
        subq = (
            select(
                Chat.character_id.label("character_id"),
                sort_at,
            )
            .where(
                Chat.user_id == user_id,
                Chat.state == ChatState.ACTIVE.value,
                or_(has_interacted, has_active_proactive_chat),
            )
            .group_by(Chat.character_id)
            .subquery()
        )

        has_unread_proactive = (
            select(Chat.id)
            .where(
                Chat.user_id == user_id,
                Chat.character_id == subq.c.character_id,
                Chat.state == ChatState.ACTIVE.value,
                Chat.origin == ChatOriginType.PROACTIVE.value,
                or_(
                    Chat.last_read_turn_no == None,  # noqa: E711
                    Chat.last_read_turn_no < Chat.last_turn_no,
                ),
            )
            .exists()
        )

        stmt = (
            select(Character, User.username, subq.c.sort_at, has_unread_proactive)
            .join(subq, Character.id == subq.c.character_id)
            .outerjoin(User, Character.creator_id == User.id)
            .order_by(desc(subq.c.sort_at), desc(Character.id))
            .offset(skip)
            .limit(limit)
        )

        result = await self._db.execute(stmt)
        return [(row[0], row[1], row[2], bool(row[3])) for row in result.all()]

    async def mark_proactive_chat_read(self, chat: Chat) -> None:
        if chat.origin == ChatOriginType.PROACTIVE.value and (
            chat.last_read_turn_no is None or chat.last_read_turn_no < chat.last_turn_no
        ):
            chat.last_read_turn_no = chat.last_turn_no

    async def archive_stale_unreplied_proactive_chats(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        keep_chat_id: uuid.UUID,
        archived_at: datetime,
    ) -> int:
        has_user_turn = (
            select(Turn.id)
            .where(
                Turn.chat_id == Chat.id,
                Turn.author_type == AuthorType.USER.value,
            )
            .exists()
        )
        stmt = (
            update(Chat)
            .where(
                Chat.user_id == user_id,
                Chat.character_id == character_id,
                Chat.id != keep_chat_id,
                Chat.state == ChatState.ACTIVE.value,
                Chat.origin == ChatOriginType.PROACTIVE.value,
                ~has_user_turn,
            )
            .values(
                state=ChatState.ARCHIVED.value,
                archived_at=archived_at,
                updated_at=func.now(),
            )
        )
        result = await self._db.execute(stmt)
        return int(result.rowcount or 0)
