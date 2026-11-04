from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.repositories.character_repository import CharacterRepository


@pytest.mark.asyncio
async def test_list_market_orders_by_created_at_then_id() -> None:
    repo = CharacterRepository.__new__(CharacterRepository)
    repo._db = AsyncMock()
    repo._db.execute = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    repo._db.execute.return_value = result

    await repo.list_market(skip=0, limit=20)

    stmt = repo._db.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "characters.visibility = 'PUBLIC'" in compiled
    assert "characters.status = 'ACTIVE'" in compiled
    assert "ORDER BY characters.created_at ASC, characters.id ASC" in compiled
