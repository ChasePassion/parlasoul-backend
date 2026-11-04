"""Server-originated realtime control events."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class SessionCreatedEvent(BaseModel):
    type: str = "session.created"
    session_id: str
    chat_id: str
    character_id: str


class ConversationTurnCreatedEvent(BaseModel):
    type: str = "conversation.turn.created"
    chat_id: str
    user_turn_id: str
    user_candidate_id: str
    assistant_turn_id: str
    assistant_candidate_id: str
    generation_id: int | None = None


class ConversationTurnUpdatedEvent(BaseModel):
    type: str = "conversation.turn.updated"
    chat_id: str
    assistant_turn_id: str
    assistant_candidate_id: str
    is_final: bool
    generation_id: int | None = None


class ConversationTurnFailedEvent(BaseModel):
    type: str = "conversation.turn.failed"
    chat_id: str
    assistant_turn_id: str
    assistant_candidate_id: str
    code: str
    message: str
    generation_id: int | None = None


class RealtimeSessionCreateRequest(BaseModel):
    chat_id: str
    character_id: str
    observation_id: uuid.UUID | None = None
    sdp: dict[str, Any]


class RealtimeIceServer(BaseModel):
    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


class RealtimeIceConfigResponse(BaseModel):
    ice_servers: list[RealtimeIceServer] = Field(default_factory=list)
    credential_ttl_seconds: int = Field(default=0, ge=0)


class RealtimeSessionCreateResponse(BaseModel):
    session_id: str
    chat_id: str
    character_id: str
    sdp: dict[str, Any]
    ice_servers: list[RealtimeIceServer] = Field(default_factory=list)
    credential_ttl_seconds: int = Field(default=0, ge=0)
