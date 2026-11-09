"""Repository for character-forge sessions and messages."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import Character, CharacterForgeMessage, CharacterForgeSession


class CharacterForgeRepository:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def create_session(self, *, user_id: uuid.UUID) -> CharacterForgeSession:
        session = CharacterForgeSession(user_id=user_id)
        self._db.add(session)
        await self._db.commit()
        await self._db.refresh(session)
        return session

    async def get_session(
        self, session_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> CharacterForgeSession | None:
        stmt = select(CharacterForgeSession).where(
            CharacterForgeSession.id == session_id,
            CharacterForgeSession.user_id == user_id,
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_sessions(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        cursor: datetime | None = None,
    ) -> list[CharacterForgeSession]:
        stmt = select(CharacterForgeSession).where(CharacterForgeSession.user_id == user_id)
        if cursor is not None:
            stmt = stmt.where(CharacterForgeSession.updated_at < cursor)
        stmt = stmt.order_by(CharacterForgeSession.updated_at.desc()).limit(limit)
        return list((await self._db.execute(stmt)).scalars().all())

    async def touch_session(
        self,
        session: CharacterForgeSession,
        *,
        title: str | None = None,
        active_character_id: uuid.UUID | None = None,
        updated_at: datetime | None = None,
    ) -> CharacterForgeSession:
        if title is not None:
            session.title = title
        if active_character_id is not None:
            session.active_character_id = active_character_id
        if updated_at is not None:
            session.updated_at = updated_at
        self._db.add(session)
        await self._db.commit()
        await self._db.refresh(session)
        return session

    async def delete_session(self, session: CharacterForgeSession) -> None:
        await self._db.execute(
            delete(CharacterForgeSession).where(CharacterForgeSession.id == session.id)
        )
        await self._db.commit()

    async def next_seq(self, session_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(CharacterForgeMessage.seq), 0)).where(
            CharacterForgeMessage.session_id == session_id
        )
        current = (await self._db.execute(stmt)).scalar_one()
        return int(current) + 1

    async def add_message(
        self,
        *,
        session_id: uuid.UUID,
        seq: int,
        role: str,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        ui_payloads: list[dict[str, Any]] | None = None,
        images: list[dict[str, Any]] | None = None,
        audios: list[dict[str, Any]] | None = None,
    ) -> CharacterForgeMessage:
        message = CharacterForgeMessage(
            session_id=session_id,
            seq=seq,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            ui_payloads=ui_payloads or [],
            images=images,
            audios=audios,
        )
        self._db.add(message)
        await self._db.commit()
        await self._db.refresh(message)
        return message

    async def update_message(
        self,
        message: CharacterForgeMessage,
        *,
        content: str | None = None,
        ui_payloads: list[dict[str, Any]] | None = None,
    ) -> CharacterForgeMessage:
        if content is not None:
            message.content = content
        if ui_payloads is not None:
            message.ui_payloads = ui_payloads
        self._db.add(message)
        await self._db.commit()
        await self._db.refresh(message)
        return message

    async def list_messages(
        self,
        session_id: uuid.UUID,
        *,
        limit: int = 200,
    ) -> list[CharacterForgeMessage]:
        stmt = (
            select(CharacterForgeMessage)
            .where(CharacterForgeMessage.session_id == session_id)
            .order_by(CharacterForgeMessage.seq.asc())
            .limit(limit)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def last_user_message_text(self, session_id: uuid.UUID) -> str | None:
        stmt = (
            select(CharacterForgeMessage.content)
            .where(
                CharacterForgeMessage.session_id == session_id,
                CharacterForgeMessage.role == "user",
            )
            .order_by(CharacterForgeMessage.seq.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def characters_by_ids(
        self, character_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, Character]:
        if not character_ids:
            return {}
        stmt = select(Character).where(Character.id.in_(character_ids))
        rows = (await self._db.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}
