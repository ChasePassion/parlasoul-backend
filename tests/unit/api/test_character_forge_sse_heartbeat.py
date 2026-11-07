"""SSE comment-frame heartbeat for the character-forge stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.api.routers.character_forge import (
    _SSE_HEARTBEAT_FRAME,
    _with_sse_heartbeat,
)


async def _collect(source: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in source]


async def test_idle_gap_emits_heartbeats_without_dropping_frames():
    async def source():
        yield "data: first\n\n"
        await asyncio.sleep(0.32)
        yield "data: second\n\n"

    frames = await _collect(_with_sse_heartbeat(source(), interval=0.1))

    assert [frame for frame in frames if frame.startswith("data:")] == [
        "data: first\n\n",
        "data: second\n\n",
    ]
    assert frames.count(_SSE_HEARTBEAT_FRAME) >= 2


async def test_fast_source_emits_no_heartbeat():
    async def source():
        yield "data: a\n\n"
        yield "data: b\n\n"

    frames = await _collect(_with_sse_heartbeat(source(), interval=0.05))

    assert frames == ["data: a\n\n", "data: b\n\n"]


async def test_cancellation_closes_source():
    closed = asyncio.Event()

    async def source():
        try:
            await asyncio.sleep(60)
            yield "data: never\n\n"
        finally:
            closed.set()

    task = asyncio.create_task(
        _collect(_with_sse_heartbeat(source(), interval=0.05))
    )
    await asyncio.sleep(0.12)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    await asyncio.wait_for(closed.wait(), timeout=1.0)


async def test_source_iteration_stays_in_one_context():
    """ContextVar tokens set inside the source must be reset in the same context."""
    import contextvars

    var = contextvars.ContextVar("forge_heartbeat_test", default=None)

    async def source():
        token = var.set("value")
        try:
            await asyncio.sleep(0.16)
            yield "data: a\n\n"
        finally:
            var.reset(token)

    frames = [
        frame
        async for frame in _with_sse_heartbeat(source(), interval=0.05)
    ]

    assert "data: a\n\n" in frames
