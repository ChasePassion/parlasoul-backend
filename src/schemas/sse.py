"""SSE event payload contracts for streaming endpoints."""

from __future__ import annotations

from typing import Literal

from sqlmodel import SQLModel

from src.schemas.growth import GrowthShareCard, GrowthTodaySummary
from src.schemas.learning import ReplyCard, ReplySuggestion


class SSEMetaTurn(SQLModel):
    id: str
    turn_no: int
    candidate_id: str


class SSEMetaEvent(SQLModel):
    type: Literal["meta"]
    user_turn: SSEMetaTurn
    assistant_turn: SSEMetaTurn


class SSETransformChunkEvent(SQLModel):
    type: Literal["transform_chunk"]
    content: str


class SSETransformDoneEvent(SQLModel):
    type: Literal["transform_done"]
    original_content: str
    transformed_content: str
    applied: bool


class SSEChunkEvent(SQLModel):
    type: Literal["chunk"]
    content: str


class SSEDoneEvent(SQLModel):
    type: Literal["done"]
    full_content: str
    assistant_turn_id: str | None = None
    assistant_candidate_id: str | None = None


class SSEChatTitleUpdatedEvent(SQLModel):
    type: Literal["chat_title_updated"]
    chat_id: str
    title: str


class SSEReplySuggestionsEvent(SQLModel):
    type: Literal["reply_suggestions"]
    assistant_candidate_id: str
    suggestions: list[ReplySuggestion]


class SSEReplyCardEvent(SQLModel):
    type: Literal["reply_card"]
    assistant_candidate_id: str
    reply_card: ReplyCard


class SSEReplyCardStartedEvent(SQLModel):
    type: Literal["reply_card_started"]
    assistant_candidate_id: str


class SSEReplyCardErrorEvent(SQLModel):
    type: Literal["reply_card_error"]
    assistant_candidate_id: str
    code: str
    message: str


class SSEMemoryQueuedEvent(SQLModel):
    type: Literal["memory_queued"]
    status: Literal["queued"]


class SSEErrorEvent(SQLModel):
    type: Literal["error"]
    code: str
    message: str


class SSETTSAudioDeltaEvent(SQLModel):
    type: Literal["tts_audio_delta"]
    assistant_candidate_id: str
    seq: int
    audio_b64: str
    mime_type: str


class SSETTSAudioDoneEvent(SQLModel):
    type: Literal["tts_audio_done"]
    assistant_candidate_id: str


class SSETTSErrorEvent(SQLModel):
    type: Literal["tts_error"]
    code: str
    message: str


class SSEGrowthDailyUpdatedEvent(SQLModel):
    type: Literal["growth_daily_updated"]
    today: GrowthTodaySummary


class SSEGrowthShareCardReadyEvent(SQLModel):
    type: Literal["growth_share_card_ready"]
    share_card: GrowthShareCard
