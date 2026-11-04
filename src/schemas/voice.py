"""Schemas for Phase 2 voice endpoints."""

from __future__ import annotations

from typing import Literal

from sqlmodel import SQLModel


class STTTranscriptionResult(SQLModel):
    text: str
    model: str
    request_id: str | None = None


class TTSAudioStreamMeta(SQLModel):
    assistant_candidate_id: str
    mime_type: Literal["audio/opus", "audio/mpeg"]

