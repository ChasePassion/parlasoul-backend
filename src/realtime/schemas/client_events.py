"""Client-originated realtime control events."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class SessionUpdateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["session.update"]
    translation_enabled: bool | None = None


class SessionPingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["session.ping"]


class ResponseCancelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["response.cancel"]


class InputAudioBufferMuteEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_audio_buffer.mute"]


class InputAudioBufferUnmuteEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_audio_buffer.unmute"]
