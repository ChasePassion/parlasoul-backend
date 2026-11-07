"""Async unit-of-work helpers to isolate session operations from services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlmodel.ext.asyncio.session import AsyncSession


class UnitOfWork:
    """Thin wrapper around AsyncSession transaction primitives."""

    def __init__(self, db: AsyncSession):
        self._db = db

    @asynccontextmanager
    async def transactional(self) -> AsyncIterator[None]:
        """Provide commit/rollback boundaries for service operations."""
        try:
            yield
        except Exception:
            await self.rollback()
            raise
        else:
            await self.commit()

    async def flush(self) -> None:
        await self._db.flush()

    async def refresh(self, instance: object) -> None:
        await self._db.refresh(instance)

    async def commit(self) -> None:
        await self._db.commit()

    async def rollback(self) -> None:
        await self._db.rollback()

