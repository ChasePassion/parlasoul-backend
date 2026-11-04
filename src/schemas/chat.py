"""Chat schemas for chat instances, turns, and streaming endpoints."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel

from src.schemas.character import CharacterStatusEnum
from src.schemas.learning import CandidateExtra
from src.schemas.media import AvatarUrls


class CharacterBrief(SQLModel):
    """Minimal character info needed to render a chat UI."""

    id: str
    name: str
    description: str
    greeting_message: str | None = None
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    status: CharacterStatusEnum = CharacterStatusEnum.ACTIVE
    unpublished_at: datetime | None = None
    visibility: str
    creator_id: str | None = None
    creator_username: str | None = None
    has_unread_proactive: bool = False


class ChatResponse(SQLModel):
    """Chat instance response model."""

    id: str
    user_id: str
    character_id: str
    type: str
    state: str
    visibility: str
    origin: str
    title: str
    last_turn_at: datetime | None = None
    last_turn_id: str | None = None
    last_turn_no: int | None = None
    active_leaf_turn_id: str | None = None
    last_read_turn_no: int | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    meta: dict[str, Any] | None = None


class ChatImageRef(SQLModel):
    """Single image reference with original and display URLs."""

    image_key: str = Field(min_length=1)
    original: str
    display: str


class CandidateResponse(SQLModel):
    """Primary candidate for a turn (Phase 1 only)."""

    id: str
    candidate_no: int
    content: str
    model_type: str | None = None
    is_final: bool = True
    rank: int | None = None
    created_at: datetime
    extra: CandidateExtra | None = None
    chat_images: list[ChatImageRef] | None = None


class TurnResponse(SQLModel):
    """Turn response with its primary candidate."""

    id: str
    turn_no: int
    author_type: str
    state: str
    is_proactive: bool
    source: str | None = None
    parent_turn_id: str | None = None
    parent_candidate_id: str | None = None
    primary_candidate: CandidateResponse
    candidate_count: int = 1


class ChatDetailResponse(SQLModel):
    """Chat + character info for rendering."""

    chat: ChatResponse
    character: CharacterBrief


class ChatHistoryItemResponse(SQLModel):
    """History list item with chat and character info."""

    chat: ChatResponse
    character: CharacterBrief


class ChatCreateRequest(SQLModel):
    """Request model for creating a chat instance."""

    character_id: uuid.UUID = Field(..., description="Character ID")
    meta: dict[str, Any] | None = Field(None, description="Optional chat metadata")


class ChatCreateResponse(SQLModel):
    """Create chat response; may include greeting turn."""

    chat: ChatResponse
    character: CharacterBrief
    initial_turns: list[TurnResponse] = Field(default_factory=list)


class ChatsPageResponse(SQLModel):
    """Chat history page response."""

    items: list[ChatHistoryItemResponse] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class TurnsPageResponse(SQLModel):
    """Turns pagination response (cursor by turn id along the active branch)."""

    chat: ChatResponse
    character: CharacterBrief
    turns: list[TurnResponse] = Field(default_factory=list)
    next_before_turn_id: str | None = None
    has_more: bool = False


# When adding new fields (images, audio, etc.), keep in sync with
# UserTurnEditStreamRequest in turns.py and the edit/regen paths in turn_service.py.
class ChatStreamRequest(SQLModel):
    """Request model for streaming message generation."""
    model_config = ConfigDict(extra="forbid")

    content: str = Field(..., min_length=1, description="User message content")
    client_message_id: str | None = Field(
        default=None,
        description="Optional client idempotency key",
    )
    images: list[ChatImageRef] = Field(
        default_factory=list,
        max_length=5,
        description="Image attachments (max 5)",
    )


class ChatUpdateRequest(SQLModel):
    """Manual chat rename request."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(
        ...,
        max_length=120,
        description="Manual chat title; null or empty-after-trim is invalid",
    )
