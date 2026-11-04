"""Minimal async event bus for realtime sessions."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from src.realtime.pipeline.events import EventType, RealtimeEvent


class RealtimeEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[EventType, set[asyncio.Queue[RealtimeEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, event: RealtimeEvent) -> None:
        async with self._lock:
            subscribers = list(self._subscribers.get(event.type, set()))

        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    @asynccontextmanager
    async def subscribe(self, event_type: EventType) -> AsyncIterator[asyncio.Queue[RealtimeEvent]]:
        queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers[event_type].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers[event_type].discard(queue)
