from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager

import pytest

from src.realtime.bridge.event_bridge import EventBridge
from src.realtime.pipeline.events import RealtimeEvent


class _TrackingQueue(asyncio.Queue[RealtimeEvent]):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled_get_count = 0

    async def get(self) -> RealtimeEvent:
        try:
            return await super().get()
        except asyncio.CancelledError:
            self.cancelled_get_count += 1
            raise


class _TrackingBus:
    def __init__(self) -> None:
        self.queues: dict[str, _TrackingQueue] = defaultdict(_TrackingQueue)

    @asynccontextmanager
    async def subscribe(self, event_type: str):
        yield self.queues[event_type]


@pytest.mark.asyncio
async def test_event_bridge_keeps_other_event_consumers_alive_between_sends() -> None:
    bus = _TrackingBus()
    sent: list[dict] = []
    sent_event = asyncio.Event()

    async def send_json(payload: dict) -> None:
        sent.append(payload)
        sent_event.set()

    bridge = EventBridge(bus=bus, send_json=send_json)
    await bridge.start()
    for _ in range(20):
        if "response_started" in bus.queues:
            break
        await asyncio.sleep(0)

    await bus.queues["response_started"].put(
        RealtimeEvent("response_started", {"generation_id": 1})
    )
    await asyncio.wait_for(sent_event.wait(), timeout=1)

    assert sent == [{"type": "response.started", "generation_id": 1}]
    assert sum(queue.cancelled_get_count for queue in bus.queues.values()) == 0

    await bridge.stop()
