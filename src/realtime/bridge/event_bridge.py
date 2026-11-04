"""Map internal realtime events to websocket data channel payloads."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

from src.realtime.pipeline.bus import RealtimeEventBus

logger = logging.getLogger(__name__)

SendJson = Callable[[dict], Awaitable[None]]


class EventBridge:
    def __init__(self, *, bus: RealtimeEventBus, send_json: SendJson) -> None:
        self._bus = bus
        self._send_json = send_json
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="realtime.event_bridge")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        async with AsyncExitStack() as stack:
            speech_started = await stack.enter_async_context(self._bus.subscribe("speech_started"))
            speech_stopped = await stack.enter_async_context(self._bus.subscribe("speech_stopped"))
            response_started = await stack.enter_async_context(self._bus.subscribe("response_started"))
            response_interrupted = await stack.enter_async_context(self._bus.subscribe("response_interrupted"))
            response_audio_stopped = await stack.enter_async_context(
                self._bus.subscribe("response_audio_stopped")
            )
            response_done = await stack.enter_async_context(self._bus.subscribe("response_done"))
            conversation_turn_created = await stack.enter_async_context(
                self._bus.subscribe("conversation_turn_created")
            )
            conversation_turn_updated = await stack.enter_async_context(
                self._bus.subscribe("conversation_turn_updated")
            )
            conversation_turn_failed = await stack.enter_async_context(
                self._bus.subscribe("conversation_turn_failed")
            )
            subtitle_delta = await stack.enter_async_context(self._bus.subscribe("subtitle_delta"))
            subtitle_final = await stack.enter_async_context(self._bus.subscribe("subtitle_final"))
            errors = await stack.enter_async_context(self._bus.subscribe("error"))
            session_updates = await stack.enter_async_context(self._bus.subscribe("session_updated"))

            queues = (
                speech_started,
                speech_stopped,
                response_started,
                response_interrupted,
                response_audio_stopped,
                response_done,
                conversation_turn_created,
                conversation_turn_updated,
                conversation_turn_failed,
                subtitle_delta,
                subtitle_final,
                errors,
                session_updates,
            )
            pending = {
                asyncio.create_task(queue.get()): queue
                for queue in queues
            }
            try:
                while not self._stop_event.is_set():
                    done, _ = await asyncio.wait(
                        pending,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        queue = pending.pop(task)
                        event = task.result()
                        # Keep one persistent consumer per event type. Cancelling
                        # every other get() here can lose an event that has already
                        # left its queue but has not returned to this task yet.
                        pending[asyncio.create_task(queue.get())] = queue
                        try:
                            await self._send_json(
                                self._event_to_payload(event.type, event.payload)
                            )
                        except Exception as exc:  # pragma: no cover - defensive
                            logger.warning(
                                "Realtime event send failed: error_type=%s",
                                type(exc).__name__,
                            )
            finally:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    def _event_to_payload(self, event_type: str, payload: dict) -> dict:
        mapping = {
            "speech_started": {"type": "input_audio_buffer.speech_started"},
            "speech_stopped": {"type": "input_audio_buffer.speech_stopped"},
            "response_started": {"type": "response.started"},
            "response_interrupted": {"type": "response.interrupted"},
            "response_audio_stopped": {"type": "response.audio_stopped"},
            "response_done": {"type": "response.done"},
            "conversation_turn_created": {"type": "conversation.turn.created"},
            "conversation_turn_updated": {"type": "conversation.turn.updated"},
            "conversation_turn_failed": {"type": "conversation.turn.failed"},
            "subtitle_delta": {"type": "subtitle.delta"},
            "subtitle_final": {"type": "subtitle.final"},
            "error": {"type": "error"},
            "session_updated": {"type": "session.updated"},
        }
        base = mapping.get(event_type, {"type": event_type})
        return {**base, **payload}
