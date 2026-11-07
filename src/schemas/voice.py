"""Schemas for Phase 2 voice endpoints."""

from __future__ import annotations

from sqlmodel import SQLModel


class STTTranscriptionResult(SQLModel):
    text: str
    model: str
    request_id: str | None = None

