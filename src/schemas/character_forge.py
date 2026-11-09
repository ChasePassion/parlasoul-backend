"""Request/response schemas for the conversational character forge."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import model_validator
from sqlmodel import Field, SQLModel

from src.schemas.chat import ChatImageRef


class ForgeAudioRef(SQLModel):
    """A recording the user attached to a forge turn."""

    audio_key: str = Field(..., min_length=1, max_length=300)
    name: str = Field(default="", max_length=160)
    duration_seconds: float = Field(default=0.0, ge=0.0)


class ForgeStreamRequest(SQLModel):
    model_config = {"extra": "forbid"}

    content: str = Field(default="", max_length=4000)
    images: list[ChatImageRef] | None = Field(default=None, max_length=5)
    audios: list[ForgeAudioRef] | None = Field(default=None, max_length=3)

    @model_validator(mode="after")
    def _require_payload(self) -> ForgeStreamRequest:
        if not self.content.strip() and not self.images and not self.audios:
            raise ValueError("content or attachments are required")
        return self


class ForgeSteerRequest(SQLModel):
    model_config = {"extra": "forbid"}

    content: str = Field(..., min_length=1, max_length=4000)
    steer_id: str | None = Field(None, min_length=8, max_length=64)


class ForgeSteerResult(SQLModel):
    steer_id: str
    queue_length: int


class ForgeSessionRenameRequest(SQLModel):
    model_config = {"extra": "forbid"}

    title: str = Field(..., min_length=1, max_length=120)


class ForgeSessionSummary(SQLModel):
    id: str
    title: str
    updated_at: datetime | None = None
    active_character_id: str | None = None
    character_name: str | None = None
    character_avatar_url: str | None = None
    last_message: str | None = None


class ForgeMessageItem(SQLModel):
    id: str
    seq: int
    role: str
    content: str | None = None
    ui_payloads: list[dict[str, Any]] = Field(default_factory=list)
    images: list[ChatImageRef] = Field(default_factory=list)
    audios: list[ForgeAudioRef] = Field(default_factory=list)
    created_at: datetime | None = None


class ForgeSessionDetail(SQLModel):
    session: ForgeSessionSummary
    items: list[ForgeMessageItem]


class ForgeSessionList(SQLModel):
    items: list[ForgeSessionSummary]
    next_cursor: str | None = None
