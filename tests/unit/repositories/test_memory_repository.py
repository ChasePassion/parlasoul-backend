from __future__ import annotations


import pytest

from src.repositories.memory_repository import MemoryRepository


class _FakeMemory:
    def list_all_for_user_character(
        self,
        *,
        user_id: str,
        character_id: str,
        semantic_limit: int,
        episodic_limit: int,
    ):
        return {"semantic": [user_id], "episodic": [character_id, semantic_limit, episodic_limit]}

    def count_for_user_character(self, *, user_id: str, character_id: str) -> int:
        return 7

    async def record_proactive_outreach_async(
        self,
        *,
        proactive_message: str,
        user_id: str,
        character_id: str,
        chat_id: str,
    ) -> int | None:
        return 42


@pytest.mark.asyncio
async def test_memory_repository_wraps_proactive_prompt_listing() -> None:
    repo = MemoryRepository(memory=_FakeMemory())

    result = await repo.list_for_proactive_prompt(
        user_id="user-1",
        character_id="character-1",
        semantic_limit=3,
        episodic_limit=4,
    )

    assert result["semantic"] == ["user-1"]
    assert result["episodic"] == ["character-1", 3, 4]


@pytest.mark.asyncio
async def test_memory_repository_wraps_count_and_record_proactive_outreach() -> None:
    repo = MemoryRepository(memory=_FakeMemory())

    count = await repo.count_by_user_character(user_id="u", character_id="c")
    memory_id = await repo.record_proactive_outreach(
        proactive_message="hi",
        user_id="u",
        character_id="c",
        chat_id="chat-1",
    )

    assert count == 7
    assert memory_id == 42
