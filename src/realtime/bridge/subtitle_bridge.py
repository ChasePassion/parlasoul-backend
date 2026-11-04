"""Subtitle event helpers."""

from __future__ import annotations

from src.realtime.pipeline.bus import RealtimeEventBus
from src.realtime.pipeline.events import RealtimeEvent


class SubtitleBridge:
    def __init__(self, bus: RealtimeEventBus) -> None:
        self._bus = bus

    async def publish_delta(
        self,
        *,
        channel: str,
        text: str,
        generation_id: int | None = None,
    ) -> None:
        if not text:
            return
        payload: dict[str, object] = {"channel": channel, "text": text}
        if generation_id is not None:
            payload["generation_id"] = generation_id
        await self._bus.publish(RealtimeEvent("subtitle_delta", payload))

    async def publish_final(
        self,
        *,
        channel: str,
        text: str,
        generation_id: int | None = None,
    ) -> None:
        if not text:
            return
        payload: dict[str, object] = {"channel": channel, "text": text}
        if generation_id is not None:
            payload["generation_id"] = generation_id
        await self._bus.publish(RealtimeEvent("subtitle_final", payload))
