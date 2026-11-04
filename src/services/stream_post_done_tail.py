"""Shared helpers for post-`done` SSE tail work.

This module coordinates the non-blocking learning artifacts that may arrive
after the assistant text stream has already emitted the `done` event.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from src.observability import capture_context_snapshot, create_task_in_observability_context
from src.schemas.learning import ReplyCardCore, ReplySuggestion


@dataclass(frozen=True)
class ReplyCardCoreReady:
    assistant_candidate_id: str
    card_core: ReplyCardCore


PostDoneQueueItem = dict[str, Any] | ReplyCardCoreReady


class PostDoneTailTasks:
    def __init__(
        self,
        *,
        queue: asyncio.Queue[PostDoneQueueItem],
        tasks: tuple[asyncio.Task[None], ...],
    ) -> None:
        self._queue = queue
        self._tasks = tasks

    def drain_nowait(self) -> list[PostDoneQueueItem]:
        items: list[PostDoneQueueItem] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    async def wait_for_item(self, timeout_s: float | None) -> PostDoneQueueItem | None:
        if not self.pending:
            return None
        if timeout_s is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None

    @property
    def pending(self) -> bool:
        return (not self._queue.empty()) or any(not task.done() for task in self._tasks)

    async def aclose(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def start_post_done_tail_tasks(
    *,
    assistant_candidate_id: str,
    suggestions_coro: asyncio.Future[list[ReplySuggestion]] | asyncio.Task[list[ReplySuggestion]] | Any,
    logger: logging.Logger,
    suggestions_error_log_message: str,
    reply_card_core_coro: asyncio.Future[ReplyCardCore | None]
    | asyncio.Task[ReplyCardCore | None]
    | Any
    | None = None,
    reply_card_error_log_message: str | None = None,
) -> PostDoneTailTasks:
    queue: asyncio.Queue[PostDoneQueueItem] = asyncio.Queue()
    tasks: list[asyncio.Task[None]] = []
    snapshot = capture_context_snapshot()

    async def _suggestions_worker() -> None:
        try:
            suggestions = await suggestions_coro
        except Exception:
            logger.exception(suggestions_error_log_message)
            return

        await queue.put(
            {
                "type": "reply_suggestions",
                "assistant_candidate_id": assistant_candidate_id,
                "suggestions": [item.model_dump() for item in suggestions],
            }
        )

    tasks.append(
        create_task_in_observability_context(
            _suggestions_worker(),
            name=f"post_done.reply_suggestions.{assistant_candidate_id}",
            snapshot=snapshot,
        )
    )

    if reply_card_core_coro is not None:

        async def _reply_card_worker() -> None:
            try:
                card_core = await reply_card_core_coro
            except Exception:
                logger.exception(reply_card_error_log_message or "Reply card generation failed")
                await queue.put(
                    {
                        "type": "reply_card_error",
                        "assistant_candidate_id": assistant_candidate_id,
                        "code": "reply_card_generation_failed",
                        "message": "reply card generation failed",
                    }
                )
                return

            if card_core is None:
                await queue.put(
                    {
                        "type": "reply_card_error",
                        "assistant_candidate_id": assistant_candidate_id,
                        "code": "reply_card_generation_failed",
                        "message": "reply card generation returned invalid payload",
                    }
                )
                return

            await queue.put(
                ReplyCardCoreReady(
                    assistant_candidate_id=assistant_candidate_id,
                    card_core=card_core,
                )
            )

        tasks.append(
            create_task_in_observability_context(
                _reply_card_worker(),
                name=f"post_done.reply_card.{assistant_candidate_id}",
                snapshot=snapshot,
            )
        )

    return PostDoneTailTasks(queue=queue, tasks=tuple(tasks))
