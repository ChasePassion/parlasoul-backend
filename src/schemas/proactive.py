"""Schemas for proactive message preferences and dispatch tooling."""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel

from src.schemas.chat import CharacterBrief


class ProactiveCharacterPreferenceItem(SQLModel):
    character: CharacterBrief
    enabled: bool
    last_interaction_at: datetime | None = None


class ProactiveCharacterPreferenceListResponse(SQLModel):
    items: list[ProactiveCharacterPreferenceItem] = Field(default_factory=list)


class ReplaceProactiveCharacterPreferencesRequest(SQLModel):
    model_config = ConfigDict(extra="forbid")

    character_ids: list[str] = Field(default_factory=list, max_length=500)
