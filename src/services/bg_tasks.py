"""Helpers for fire-and-forget background tasks.

We use these helpers to:
- ensure exceptions are always retrieved (avoid "Task exception was never retrieved")
- log failures with context for debugging
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, Optional

from src.observability import capture_context_snapshot, create_task_in_observability_context

logger = logging.getLogger(__name__)


def spawn_bg_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    ctx: Optional[dict[str, Any]] = None,
) -> asyncio.Task[Any]:
    snapshot = capture_context_snapshot()
    task = create_task_in_observability_context(coro, name=name, snapshot=snapshot)

    def _done(t: asyncio.Task[Any]) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Background task callback failed. name=%s ctx=%s", name, ctx)
            return

        if exc is not None:
            logger.error("Background task failed. name=%s ctx=%s", name, ctx, exc_info=exc)

    task.add_done_callback(_done)
    return task
