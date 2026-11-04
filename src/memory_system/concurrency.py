"""Concurrency helpers for the memory system.

The memory system uses several blocking libraries (Milvus client, sync SDK calls).
To keep the FastAPI event loop responsive, we run those calls in AnyIO's worker thread
pool with a dedicated concurrency limiter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar

import anyio

from src.core.config import settings
from src.observability import capture_context_snapshot

T = TypeVar("T")

_MEMORY_THREAD_LIMITER: anyio.CapacityLimiter | None = None


def get_memory_thread_limiter() -> anyio.CapacityLimiter:
    """Return a shared limiter for memory-related blocking calls.

    Default to 4 workers (user requirement) but allow override via env var.
    """
    global _MEMORY_THREAD_LIMITER
    if _MEMORY_THREAD_LIMITER is None:
        max_workers = settings.MEMORY_MAX_WORKERS if settings.MEMORY_MAX_WORKERS > 0 else 4
        _MEMORY_THREAD_LIMITER = anyio.CapacityLimiter(max_workers)
    return _MEMORY_THREAD_LIMITER


async def run_blocking(
    func: Callable[..., T],
    /,
    *args: Any,
    timeout_s: float | None = None,
    abandon_on_cancel: bool = True,
    **kwargs: Any,
) -> T:
    """Run blocking code in a worker thread with a shared concurrency limit.

    Notes:
    - `timeout_s` applies to awaiting the result; the worker thread may continue running.
    - `abandon_on_cancel=True` ensures cancellations/timeouts do not wait for the thread.
    """
    snapshot = capture_context_snapshot()
    call = partial(func, *args, **kwargs)
    wrapped_call = partial(snapshot.run, call)
    awaitable = anyio.to_thread.run_sync(
        wrapped_call,
        limiter=get_memory_thread_limiter(),
        abandon_on_cancel=abandon_on_cancel,
    )
    if timeout_s is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout=timeout_s)
