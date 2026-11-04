"""Schemas for chat learning features."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel


DisplayMode = Literal["concise", "detailed"]
ReplySuggestionType = Literal["relationship", "topic", "interaction"]
SavedItemKind = Literal["reply_card", "word_card", "feedback_card"]


class InputTransform(SQLModel):
    applied: bool
    transformed_content: str


class ReplySuggestion(SQLModel):
    type: ReplySuggestionType
    en: str
    zh: str


class ReplyCardPhrase(SQLModel):
    surface: str
    zh: str
    ipa_us: str


class CardFavoriteState(SQLModel):
    enabled: bool = True
    is_favorited: bool = False
    saved_item_id: str | None = None


class ReplyCardFavorite(CardFavoriteState):
    pass


class ReplyCardCore(SQLModel):
    surface: str
    zh: str
    key_phrases: list[ReplyCardPhrase] = Field(default_factory=list)
    model_config = ConfigDict(extra="allow")


class ReplyCard(ReplyCardCore):
    favorite: ReplyCardFavorite = Field(default_factory=ReplyCardFavorite)
    model_config = ConfigDict(extra="allow")


class WordCardSense(SQLModel):
    zh: str
    note: str | None = None


class WordCardPosGroup(SQLModel):
    pos: str
    senses: list[WordCardSense] = Field(default_factory=list)


class WordCardExample(SQLModel):
    surface: str
    zh: str


class WordCardCore(SQLModel):
    surface: str
    ipa_us: str
    pos_groups: list[WordCardPosGroup] = Field(default_factory=list)
    example: WordCardExample
    model_config = ConfigDict(extra="allow")


class WordCard(WordCardCore):
    favorite: CardFavoriteState = Field(default_factory=CardFavoriteState)
    model_config = ConfigDict(extra="allow")


class FeedbackCardCore(SQLModel):
    surface: str
    zh: str
    key_phrases: list[ReplyCardPhrase] = Field(default_factory=list)
    model_config = ConfigDict(extra="allow")


class FeedbackCard(FeedbackCardCore):
    favorite: CardFavoriteState = Field(default_factory=CardFavoriteState)
    model_config = ConfigDict(extra="allow")


class CandidateExtra(SQLModel):
    input_transform: InputTransform | None = None
    reply_card: ReplyCard | None = None
    model_config = ConfigDict(extra="allow")


class SavedItemDisplay(SQLModel):
    surface: str
    zh: str


class SavedItemSource(SQLModel):
    role_id: uuid.UUID
    chat_id: uuid.UUID
    message_id: str
    turn_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    meta: dict[str, Any] | None = None
    model_config = ConfigDict(extra="allow")


class SavedItemPayload(SQLModel):
    kind: SavedItemKind
    display: SavedItemDisplay
    card: dict[str, Any]
    source: SavedItemSource
    model_config = ConfigDict(extra="forbid")


class SavedItemCreateRequest(SQLModel):
    saved_item: SavedItemPayload
    model_config = ConfigDict(extra="forbid")


class SavedItemResponse(SavedItemPayload):
    id: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class SavedItemsPage(SQLModel):
    items: list[SavedItemResponse] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    model_config = ConfigDict(from_attributes=True)


class WordCardGenerateRequest(SQLModel):
    selected_text: str = Field(..., min_length=1, max_length=200)
    context_text: str | None = Field(default=None, max_length=4000)
    model_config = ConfigDict(extra="forbid")


class ChatContextMessage(SQLModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1)
    model_config = ConfigDict(extra="forbid")


class AssistantStreamRequest(SQLModel):
    question: str = Field(..., min_length=1)
    character_chat_context: list[ChatContextMessage] = Field(default_factory=list)
    assistant_chat_context: list[ChatContextMessage] = Field(default_factory=list)
    chat_id: uuid.UUID | None = None
    model_config = ConfigDict(extra="forbid")


class WordCardPayload(SQLModel):
    word_card: WordCard


class ReplyCardPayload(SQLModel):
    reply_card: ReplyCard


class FeedbackCardPayload(SQLModel):
    feedback_card: FeedbackCard
