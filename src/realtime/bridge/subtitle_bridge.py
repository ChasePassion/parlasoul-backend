"""Subtitle event helpers."""

from __future__ import annotations

from src.realtime.pipeline.bus import RealtimeEventBus
from src.realtime.pipeline.events import RealtimeEvent


class SubtitleBridge:
    def __init__(self, bus: RealtimeEventBus) -> None:
        self._bus = bus

    async def _publish(
        self,
        event_type: str,
        *,
        channel: str,
        text: str,
        generation_id: int | None,
    ) -> None:
        if not text:
            return
        payload: dict[str, object] = {"channel": channel, "text": text}
        if generation_id is not None:
            payload["generation_id"] = generation_id
        await self._bus.publish(RealtimeEvent(event_type, payload))

    async def publish_delta(
        self,
        *,
        channel: str,
        text: str,
        generation_id: int | None = None,
    ) -> None:
        await self._publish("subtitle_delta", channel=channel, text=text, generation_id=generation_id)

    async def publish_final(
        self,
        *,
        channel: str,
        text: str,
        generation_id: int | None = None,
    ) -> None:
        await self._publish("subtitle_final", channel=channel, text=text, generation_id=generation_id)
