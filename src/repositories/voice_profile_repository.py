"""Repository for voice profile persistence and queries."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import and_, func, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import Character, VoiceProfile, VoiceProfileStatus, VoiceSourceType


class VoiceProfileRepository:
    """Data access methods for voice profiles."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def create(self, profile: VoiceProfile) -> VoiceProfile:
        self._db.add(profile)
        await self._db.flush()
        await self._db.refresh(profile)
        return profile

    async def get_by_id(self, voice_id: uuid.UUID) -> VoiceProfile | None:
        return await self._db.get(VoiceProfile, voice_id)

    async def get_by_ids(self, voice_ids: list[uuid.UUID]) -> list[VoiceProfile]:
        if not voice_ids:
            return []
        stmt = select(VoiceProfile).where(VoiceProfile.id.in_(voice_ids))
        return list((await self._db.execute(stmt)).scalars().all())

    async def get_by_id_for_owner(
        self,
        *,
        voice_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> VoiceProfile | None:
        stmt = select(VoiceProfile).where(
            VoiceProfile.id == voice_id,
            VoiceProfile.owner_user_id == owner_user_id,
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def list_by_owner(
        self,
        *,
        owner_user_id: uuid.UUID,
        status: str | None,
        source_type: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[VoiceProfile], str | None, bool]:
        stmt = select(VoiceProfile).where(VoiceProfile.owner_user_id == owner_user_id)
        if status:
            stmt = stmt.where(VoiceProfile.status == status)
        if source_type:
            stmt = stmt.where(VoiceProfile.source_type == source_type)

        if cursor:
            try:
                cursor_id = uuid.UUID(cursor)
            except ValueError:
                cursor_id = None
            if cursor_id:
                cursor_item = await self.get_by_id_for_owner(
                    voice_id=cursor_id,
                    owner_user_id=owner_user_id,
                )
                if cursor_item:
                    cursor_created_at = cursor_item.created_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
                    stmt = stmt.where(
                        or_(
                            VoiceProfile.created_at < cursor_created_at,
                            and_(
                                VoiceProfile.created_at == cursor_created_at,
                                VoiceProfile.id < cursor_item.id,
                            ),
                        )
                    )

        stmt = stmt.order_by(VoiceProfile.created_at.desc(), VoiceProfile.id.desc()).limit(limit + 1)
        rows = list((await self._db.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = str(items[-1].id) if has_more and items else None
        return items, next_cursor, has_more

    async def list_user_ready_custom(
        self,
        *,
        user_id: uuid.UUID,
        provider: str | None,
        provider_model: str | None,
    ) -> list[VoiceProfile]:
        stmt = select(VoiceProfile).where(VoiceProfile.status == VoiceProfileStatus.READY.value)
        if provider:
            stmt = stmt.where(VoiceProfile.provider == provider)
        if provider_model:
            stmt = stmt.where(VoiceProfile.provider_model == provider_model)
        stmt = stmt.where(
            VoiceProfile.owner_user_id == user_id,
            VoiceProfile.source_type == VoiceSourceType.CLONE.value,
        )
        stmt = stmt.order_by(VoiceProfile.created_at.desc(), VoiceProfile.id.desc())

        return list((await self._db.execute(stmt)).scalars().all())

    async def get_ready_clone_by_owner_and_provider_voice(
        self,
        *,
        owner_user_id: uuid.UUID,
        provider: str,
        provider_voice_id: str,
    ) -> VoiceProfile | None:
        stmt = select(VoiceProfile).where(
            VoiceProfile.owner_user_id == owner_user_id,
            VoiceProfile.provider == provider,
            VoiceProfile.provider_voice_id == provider_voice_id,
            VoiceProfile.source_type == VoiceSourceType.CLONE.value,
            VoiceProfile.status == VoiceProfileStatus.READY.value,
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def update(self, profile: VoiceProfile, update_data: dict[str, object]) -> VoiceProfile:
        for field, value in update_data.items():
            setattr(profile, field, value)
        await self._db.flush()
        await self._db.refresh(profile)
        return profile

    async def delete(self, profile: VoiceProfile) -> None:
        await self._db.delete(profile)
        await self._db.flush()

    async def count_referenced_characters(
        self,
        *,
        provider: str,
        provider_voice_id: str,
    ) -> int:
        stmt = select(func.count(Character.id)).where(
            Character.voice_source_type == VoiceSourceType.CLONE.value,
            Character.voice_provider == provider,
            Character.voice_provider_voice_id == provider_voice_id,
        )
        value = (await self._db.execute(stmt)).scalar_one()
        return int(value or 0)
