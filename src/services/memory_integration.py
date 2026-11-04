"""Integration helpers between chat/turn services and the memory system."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.repositories.memory_repository import MemoryRepository

logger = logging.getLogger(__name__)


def _empty_memories() -> Dict[str, List[Any]]:
    # Keep the same shape as Memory.search() output.
    return {"episodic": [], "semantic": []}


async def safe_memory_search(
    memory_repo: MemoryRepository,
    *,
    query: str,
    user_id: str,
    character_id: str,
    chat_id: str,
    timeout_s: float = 3.0,
) -> Dict[str, List[Any]]:
    """Best-effort memory retrieval.

    Memory retrieval is an enhancement; chat generation must not fail when it fails.
    """
    result = await memory_repo.search_best_effort(
        query=query,
        user_id=user_id,
        character_id=character_id,
        chat_id=chat_id,
        timeout_s=timeout_s,
    )
    if not isinstance(result, dict):
        logger.warning(
            "Memory search returned unexpected payload; continuing without memories. user_id=%s character_id=%s",
            user_id,
            character_id,
        )
        return _empty_memories()
    return result
