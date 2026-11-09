"""User repository for user and creator-list access."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import Character, User, VisibilityType

_TERMINAL_ONBOARDING_STATUSES = frozenset({"completed", "skipped"})


class UserRepository:
    """Data access methods for users and creator character lists."""

    def __init__(self, db: AsyncSession):
        self._db: AsyncSession = db

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(User.id == user_id)
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def get_by_dodo_customer_id(self, dodo_customer_id: str) -> User | None:
        stmt = select(User).where(User.dodo_customer_id == dodo_customer_id)
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def authenticate_or_create_by_email(self, email: str, *, now: datetime) -> User:
        user = await self.get_by_email(email)
        if not user:
            user = User(email=email)
            self._db.add(user)

        user.last_login_at = now
        await self._db.commit()
        await self._db.refresh(user)
        return user

    async def update_profile(
        self,
        user: User,
        *,
        username: str | None,
        avatar_image_key: str | None,
        avatar_url: str | None = None,
    ) -> User:
        if username is not None:
            user.username = username
        if avatar_image_key is not None:
            user.avatar_image_key = avatar_image_key
            user.avatar_url = None
        if avatar_url is not None:
            user.avatar_url = avatar_url
            user.avatar_image_key = None

        await self._db.commit()
        await self._db.refresh(user)
        return user

    async def update_onboarding_status(self, user: User, *, status: str) -> User:
        if user.onboarding_status in _TERMINAL_ONBOARDING_STATUSES:
            return user
        if user.onboarding_status == status:
            return user

        user.onboarding_status = status
        await self._db.commit()
        await self._db.refresh(user)
        return user

    async def list_creator_characters(
        self,
        creator_id: uuid.UUID,
        *,
        skip: int,
        limit: int,
        public_only: bool,
    ) -> list[Character]:
        stmt = select(Character).where(
            Character.creator_id == creator_id,
            Character.deleted_at.is_(None),
        )
        if public_only:
            stmt = stmt.where(
                Character.visibility == VisibilityType.PUBLIC.value,
            )
        stmt = stmt.offset(skip).limit(limit)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())
