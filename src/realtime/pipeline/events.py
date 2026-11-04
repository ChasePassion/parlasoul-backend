"""Internal realtime pipeline event models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


def utc_now() -> datetime:
    return datetime.now(UTC)


EventType = Literal[
    "speech_started",
    "speech_stopped",
    "response_started",
    "response_interrupted",
    "response_audio_stopped",
    "response_done",
    "conversation_turn_created",
    "conversation_turn_updated",
    "conversation_turn_failed",
    "subtitle_delta",
    "subtitle_final",
    "session_updated",
    "error",
]


@dataclass(slots=True)
class RealtimeEvent:
    type: EventType
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=utc_now)
