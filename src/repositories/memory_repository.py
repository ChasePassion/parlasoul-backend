"""Repository adapter for memory-system operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.memory_system import Memory
from src.memory_system.concurrency import run_blocking
from src.memory_system.utils import build_filter
from src.observability import (
    build_chat_session_id,
    build_memory_batch_session_id,
    observability_scope,
)

logger = logging.getLogger(__name__)


class MemoryRepository:
    """Data access adapter around the memory subsystem."""

    def __init__(
        self,
        memory: Memory | None = None,
        *,
        memory_factory: Callable[[], Memory] | None = None,
    ):
        if memory is None and memory_factory is None:
            raise ValueError("memory or memory_factory is required")
        self._memory = memory
        self._memory_factory = memory_factory

    def _get_memory(self) -> Memory:
        if self._memory is not None:
            return self._memory

        assert self._memory_factory is not None
        try:
            self._memory = self._memory_factory()
            return self._memory
        except APIError:
            raise
        except Exception as exc:
            logger.exception("Memory subsystem unavailable during initialization: %s", exc)
            raise APIError(
                status_code=503,
                code=ErrorCode.MEMORY_UNAVAILABLE,
                message="dependency unavailable: memory subsystem initialization failed",
            ) from exc

    @property
    def use_all_semantic(self) -> bool:
        return self._get_memory().config.use_all_semantic

    @property
    def allowed_memory_search_timeout_s(self) -> float:
        return 3.0

    async def manage(
        self,
        *,
        user_text: str,
        assistant_text: str,
        user_id: str,
        character_id: str,
        chat_id: str,
    ) -> list[int]:
        return await self._get_memory().manage_async(
            user_text=user_text,
            assistant_text=assistant_text,
            user_id=user_id,
            character_id=character_id,
            chat_id=chat_id,
        )

    async def search(
        self,
        *,
        query: str,
        user_id: str,
        character_id: str,
        chat_id: str,
        timeout_s: float | None = None,
    ) -> dict[str, list[Any]]:
        with observability_scope(
            user_id=user_id,
            session_id=build_chat_session_id(chat_id),
            tags=["memory", "search"],
            metadata={
                "operation": "memory.search",
                "chat_id": chat_id,
                "character_id": character_id,
            },
        ):
            return await run_blocking(
                self._get_memory().search,
                query=query,
                user_id=user_id,
                character_id=character_id,
                chat_id=chat_id,
                timeout_s=timeout_s,
            )

    async def search_best_effort(
        self,
        *,
        query: str,
        user_id: str,
        character_id: str,
        chat_id: str,
        timeout_s: float | None = None,
    ) -> dict[str, list[Any]]:
        try:
            return await self.search(
                query=query,
                user_id=user_id,
                character_id=character_id,
                chat_id=chat_id,
                timeout_s=timeout_s,
            )
        except BaseException:
            # NOTE: catch BaseException (not Exception) because Langfuse's @observe
            # decorator can raise KeyboardInterrupt / other BaseException subclasses
            # from inside snapshot.run that are not subclasses of Exception.
            # This matches the pattern used in llm_gateway_service for the same
            # snapshot.run call path.
            logger.warning(
                "Memory search failed; continuing without memories. user_id=%s character_id=%s",
                user_id,
                character_id,
                exc_info=True,
            )
            return {"episodic": [], "semantic": []}

    async def list_for_proactive_prompt(
        self,
        *,
        user_id: str,
        character_id: str,
        semantic_limit: int = 1000,
        episodic_limit: int = 1000,
    ) -> dict[str, list[Any]]:
        return await run_blocking(
            self._get_memory().list_all_for_user_character,
            user_id=user_id,
            character_id=character_id,
            semantic_limit=semantic_limit,
            episodic_limit=episodic_limit,
        )

    async def count_by_user_character(
        self,
        *,
        user_id: str,
        character_id: str,
    ) -> int:
        return await run_blocking(
            self._get_memory().count_for_user_character,
            user_id=user_id,
            character_id=character_id,
        )

    async def record_proactive_outreach(
        self,
        *,
        proactive_message: str,
        user_id: str,
        character_id: str,
        chat_id: str,
    ) -> int | None:
        return await self._get_memory().record_proactive_outreach_async(
            proactive_message=proactive_message,
            user_id=user_id,
            character_id=character_id,
            chat_id=chat_id,
        )

    async def judge_and_assign_narrative_group(
        self,
        *,
        episodic_memories: list[Any],
        last_user: str,
        last_assistant: str,
        user_id: str,
        character_id: str,
        chat_id: str,
    ) -> list[int]:
        with observability_scope(
            user_id=user_id,
            session_id=build_chat_session_id(chat_id),
            tags=["memory", "narrative_assign"],
            metadata={
                "operation": "memory.narrative_assign",
                "chat_id": chat_id,
                "character_id": character_id,
            },
        ):
            return await self._get_memory().judge_and_assign_narrative_group_async(
                episodic_memories=episodic_memories,
                last_user=last_user,
                last_assistant=last_assistant,
                user_id=user_id,
                character_id=character_id,
                chat_id=chat_id,
            )

    async def exists(
        self,
        *,
        memory_id: int,
        user_id: str,
        character_id: str,
    ) -> bool:
        records = await run_blocking(
            self._get_memory().store.query,
            filter_expr=build_filter(
                user_id,
                character_id,
                f"id == {memory_id}",
            ),
            output_fields=["id"],
            limit=1,
        )
        return bool(records)

    async def delete(
        self,
        *,
        memory_id: int,
        user_id: str,
        character_id: str,
    ) -> bool:
        return await run_blocking(
            self._get_memory().delete,
            memory_id=memory_id,
            user_id=user_id,
            character_id=character_id,
        )

    async def reset(
        self,
        *,
        user_id: str,
        character_id: str,
    ) -> int:
        return await run_blocking(
            self._get_memory().reset,
            user_id=user_id,
            character_id=character_id,
        )

    async def purge_character(self, *, character_id: str) -> int:
        """Delete every memory for a character across all users."""
        return await run_blocking(
            self._get_memory().purge_character,
            character_id=character_id,
        )

    async def consolidate(
        self,
        *,
        user_id: str,
        character_id: str,
        timeout_s: float | None = None,
    ) -> Any:
        with observability_scope(
            user_id=user_id,
            session_id=build_memory_batch_session_id(user_id),
            tags=["memory", "consolidate"],
            metadata={
                "operation": "memory.consolidate.batch",
                "character_id": character_id,
            },
        ):
            coro = self._get_memory().consolidate_async(
                user_id=user_id,
                character_id=character_id,
            )
            if timeout_s is None:
                return await coro
            return await asyncio.wait_for(coro, timeout=timeout_s)
