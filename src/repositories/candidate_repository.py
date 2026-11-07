"""Candidate repository for turn candidate data access."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import AuthorType, Candidate, Chat, Turn


class CandidateRepository:
    """Data access methods for candidates."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_by_id(self, candidate_id: uuid.UUID) -> Candidate | None:
        return await self._db.get(Candidate, candidate_id)

    async def get_by_turn_and_no(self, *, turn_id: uuid.UUID, candidate_no: int) -> Candidate | None:
        stmt = select(Candidate).where(
            Candidate.turn_id == turn_id,
            Candidate.candidate_no == int(candidate_no),
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def get_by_ids(self, candidate_ids: Iterable[uuid.UUID]) -> list[Candidate]:
        ids = list(candidate_ids)
        if not ids:
            return []
        stmt = select(Candidate).where(Candidate.id.in_(ids))
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_turn_id(self, *, turn_id: uuid.UUID) -> list[Candidate]:
        stmt = select(Candidate).where(Candidate.turn_id == turn_id).order_by(Candidate.candidate_no)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_max_candidate_no(self, *, turn_id: uuid.UUID) -> int:
        stmt = select(func.max(Candidate.candidate_no)).where(Candidate.turn_id == turn_id)
        value = (await self._db.execute(stmt)).scalar_one()
        return int(value or 0)

    async def count_by_turn_ids(self, turn_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
        ids = list(turn_ids)
        if not ids:
            return {}
        stmt = (
            select(Candidate.turn_id, func.count(Candidate.id))
            .where(Candidate.turn_id.in_(ids))
            .group_by(Candidate.turn_id)
        )
        rows = list((await self._db.execute(stmt)).all())
        return {turn_id: int(count) for turn_id, count in rows}

    async def get_assistant_candidate_with_chat(
        self,
        *,
        candidate_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[Candidate, Turn, Chat] | None:
        stmt = (
            select(Candidate, Turn, Chat)
            .join(Turn, Turn.id == Candidate.turn_id)
            .join(Chat, Chat.id == Turn.chat_id)
            .where(
                Candidate.id == candidate_id,
                Chat.user_id == user_id,
                Turn.author_type == AuthorType.CHARACTER.value,
            )
        )
        return (await self._db.execute(stmt)).first()

    async def get_candidate_with_turn_and_chat(
        self,
        *,
        candidate_id: uuid.UUID,
    ) -> tuple[Candidate, Turn, Chat] | None:
        stmt = (
            select(Candidate, Turn, Chat)
            .join(Turn, Turn.id == Candidate.turn_id)
            .join(Chat, Chat.id == Turn.chat_id)
            .where(Candidate.id == candidate_id)
        )
        return (await self._db.execute(stmt)).first()

    def add(self, candidate: Candidate) -> None:
        self._db.add(candidate)

