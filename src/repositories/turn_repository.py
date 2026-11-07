"""Turn repository for turn-tree data access."""

from __future__ import annotations

import uuid

from datetime import datetime

from sqlalchemy import func, select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import AuthorType, Candidate, Chat, ChatOriginType, Turn


class TurnRepository:
    """Data access methods for turns."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_by_id(self, turn_id: uuid.UUID) -> Turn | None:
        return await self._db.get(Turn, turn_id)

    async def get_for_update(self, turn_id: uuid.UUID) -> Turn | None:
        stmt = select(Turn).where(Turn.id == turn_id).with_for_update()
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def get_child_by_parent_candidate(self, parent_candidate_id: uuid.UUID) -> Turn | None:
        stmt = select(Turn).where(Turn.parent_candidate_id == parent_candidate_id)
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def get_ancestor_chain(
        self,
        *,
        leaf_turn_id: uuid.UUID,
        chat_id: uuid.UUID,
        limit: int | None = None,
    ) -> tuple[list[Turn], bool]:
        """Load the ancestor chain from a leaf turn to the root in a single recursive CTE.

        Returns (turns, has_more) where has_more is True when the chain extends
        beyond the returned turns (i.e. the oldest turn has a non-null parent_turn_id).
        When limit is given, fetches limit+1 rows to detect has_more.
        """
        cte_sql = text("""
            WITH RECURSIVE chain AS (
                SELECT * FROM turns WHERE id = :leaf_id AND chat_id = :chat_id
                UNION ALL
                SELECT t.* FROM turns t JOIN chain c ON t.id = c.parent_turn_id
                WHERE t.chat_id = :chat_id
            )
            SELECT * FROM chain LIMIT :fetch_n
        """)
        params: dict[str, object] = {
            "leaf_id": leaf_turn_id,
            "chat_id": chat_id,
            "fetch_n": limit + 1 if limit else 10000,
        }
        result = await self._db.execute(cte_sql, params)
        rows = result.mappings().all()

        has_more = False
        if limit and len(rows) > limit:
            rows = rows[:limit]
            has_more = True
        elif not limit and rows:
            has_more = rows[-1].get("parent_turn_id") is not None

        return [Turn(**row) for row in rows], has_more

    async def get_max_turn_no(self, *, chat_id: uuid.UUID) -> int:
        stmt = select(func.max(Turn.turn_no)).where(Turn.chat_id == chat_id)
        value = (await self._db.execute(stmt)).scalar_one()
        return int(value or 0)

    async def count_by_chat_and_author(self, *, chat_id: uuid.UUID, author_type: str) -> int:
        stmt = select(func.count(Turn.id)).where(
            Turn.chat_id == chat_id,
            Turn.author_type == author_type,
        )
        value = (await self._db.execute(stmt)).scalar_one()
        return int(value or 0)

    async def count_user_turns_for_character(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
        since: datetime,
        cap: int,
    ) -> int:
        stmt = (
            select(func.count(func.distinct(Turn.id)))
            .join(Chat, Chat.id == Turn.chat_id)
            .where(
                Chat.user_id == user_id,
                Chat.character_id == character_id,
                Turn.author_type == AuthorType.USER.value,
                Turn.created_at >= since,
            )
        )
        value = (await self._db.execute(stmt)).scalar_one()
        return min(int(value or 0), cap)

    async def get_last_proactive_outreach_at(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
    ) -> datetime | None:
        stmt = (
            select(func.max(Turn.created_at))
            .join(Chat, Chat.id == Turn.chat_id)
            .join(Candidate, Candidate.id == Turn.primary_candidate_id)
            .where(
                Chat.user_id == user_id,
                Chat.character_id == character_id,
                Chat.origin == ChatOriginType.PROACTIVE.value,
                Turn.author_type == AuthorType.CHARACTER.value,
                Turn.is_proactive.is_(True),
                Candidate.extra["source"].astext == "proactive_outreach",
            )
        )
        return (await self._db.execute(stmt)).scalar_one()

    async def get_last_interaction_at(
        self,
        *,
        user_id: uuid.UUID,
        character_id: uuid.UUID,
    ) -> datetime | None:
        stmt = (
            select(func.max(Turn.created_at))
            .join(Chat, Chat.id == Turn.chat_id)
            .where(
                Chat.user_id == user_id,
                Chat.character_id == character_id,
            )
        )
        return (await self._db.execute(stmt)).scalar_one()

    async def get_last_activity_at(self, *, user_id: uuid.UUID) -> datetime | None:
        stmt = (
            select(func.max(Turn.created_at))
            .join(Chat, Chat.id == Turn.chat_id)
            .where(Chat.user_id == user_id)
        )
        return (await self._db.execute(stmt)).scalar_one()

    def add(self, turn: Turn) -> None:
        self._db.add(turn)

