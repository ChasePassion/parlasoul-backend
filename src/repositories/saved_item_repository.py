"""Repository for saved item favorites."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import and_, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import SavedItem


class SavedItemRepository:
    """Data access methods for saved items."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_by_id_and_user(
        self,
        *,
        saved_item_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> SavedItem | None:
        stmt = select(SavedItem).where(
            SavedItem.id == saved_item_id,
            SavedItem.user_id == user_id,
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def find_duplicate_by_surface(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        surface: str,
    ) -> SavedItem | None:
        stmt = select(SavedItem).where(
            SavedItem.user_id == user_id,
            SavedItem.kind == (kind or "").strip(),
            SavedItem.display_surface == surface,
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def find_latest_by_surface(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        surface: str,
    ) -> SavedItem | None:
        stmt = (
            select(SavedItem)
            .where(
                SavedItem.user_id == user_id,
                SavedItem.kind == (kind or "").strip(),
                SavedItem.display_surface == surface,
            )
            .order_by(SavedItem.created_at.desc(), SavedItem.id.desc())
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def create(self, item: SavedItem) -> SavedItem:
        self._db.add(item)
        await self._db.flush()
        await self._db.refresh(item)
        return item

    async def list_recent_by_user(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
    ) -> list[SavedItem]:
        stmt = (
            select(SavedItem)
            .where(SavedItem.user_id == user_id)
            .order_by(SavedItem.created_at.desc(), SavedItem.id.desc())
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_user(
        self,
        *,
        user_id: uuid.UUID,
        kind: str | None,
        role_id: uuid.UUID | None,
        chat_id: uuid.UUID | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[SavedItem], str | None, bool]:
        stmt = select(SavedItem).where(SavedItem.user_id == user_id)
        if kind:
            stmt = stmt.where(SavedItem.kind == (kind or "").strip())
        if role_id:
            stmt = stmt.where(SavedItem.source_role_id == role_id)
        if chat_id:
            stmt = stmt.where(SavedItem.source_chat_id == chat_id)

        if cursor:
            cursor_item = await self.get_by_id_and_user(saved_item_id=uuid.UUID(cursor), user_id=user_id)
            if cursor_item:
                cursor_created_at: datetime = cursor_item.created_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
                stmt = stmt.where(
                    or_(
                        SavedItem.created_at < cursor_created_at,
                        and_(
                            SavedItem.created_at == cursor_created_at,
                            SavedItem.id < cursor_item.id,
                        ),
                    )
                )

        stmt = stmt.order_by(SavedItem.created_at.desc(), SavedItem.id.desc()).limit(limit + 1)
        result = await self._db.execute(stmt)
        rows = list(result.scalars().all())
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = str(items[-1].id) if has_more and items else None
        return items, next_cursor, has_more

    async def delete(self, item: SavedItem) -> None:
        await self._db.delete(item)
        await self._db.flush()
