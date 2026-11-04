from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.repositories.turn_repository import TurnRepository


@pytest.mark.asyncio
async def test_count_user_turns_for_character_applies_cap() -> None:
    scalar_result = SimpleNamespace(scalar_one=lambda: 99)
    session = SimpleNamespace(execute=AsyncMock(return_value=scalar_result))
    repo = TurnRepository(session)

    count = await repo.count_user_turns_for_character(
        user_id=uuid.uuid4(),
        character_id=uuid.uuid4(),
        since=datetime.now(UTC),
        cap=10,
    )

    assert count == 10
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_last_proactive_outreach_at_returns_scalar_value() -> None:
    expected = datetime.now(UTC)
    scalar_result = SimpleNamespace(scalar_one=lambda: expected)
    session = SimpleNamespace(execute=AsyncMock(return_value=scalar_result))
    repo = TurnRepository(session)

    result = await repo.get_last_proactive_outreach_at(
        user_id=uuid.uuid4(),
        character_id=uuid.uuid4(),
    )

    assert result == expected
    session.execute.assert_awaited_once()
