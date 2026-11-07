"""Request/response schemas for the conversational character forge."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlmodel import Field, SQLModel

from src.schemas.chat import ChatImageRef


class ForgeStreamRequest(SQLModel):
    model_config = {"extra": "forbid"}

    content: str = Field(..., min_length=1, max_length=4000)
    images: list[ChatImageRef] | None = Field(default=None, max_length=5)


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
    created_at: datetime | None = None


class ForgeSessionDetail(SQLModel):
    session: ForgeSessionSummary
    items: list[ForgeMessageItem]


class ForgeSessionList(SQLModel):
    items: list[ForgeSessionSummary]
    next_cursor: str | None = None
