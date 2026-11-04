from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.repositories.chat_repository import ChatRepository


@pytest.mark.asyncio
async def test_archive_stale_unreplied_proactive_chats_returns_rowcount() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=2)),
    )
    repo = ChatRepository(session)

    updated = await repo.archive_stale_unreplied_proactive_chats(
        user_id=uuid.uuid4(),
        character_id=uuid.uuid4(),
        keep_chat_id=uuid.uuid4(),
        archived_at=datetime.now(UTC),
    )

    assert updated == 2
    session.execute.assert_awaited_once()
