"""Voice profile schemas for API contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import Field, SQLModel

from src.schemas.media import AvatarUrls


class VoiceSourceTypeEnum(StrEnum):
    SYSTEM = "system"
    CLONE = "clone"
    DESIGNED = "designed"
    IMPORTED = "imported"


class VoiceStatusEnum(StrEnum):
    CREATING = "creating"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class VoiceSelectableItem(SQLModel):
    id: str
    display_name: str
    source_type: VoiceSourceTypeEnum
    provider: str
    provider_model: str | None = None
    provider_voice_id: str
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    preview_text: str | None = None
    preview_audio_url: str | None = None
    usage_hint: str | None = None

    model_config = {"from_attributes": True}


class VoiceProfileResponse(SQLModel):
    id: str
    owner_user_id: str | None = None
    provider: str
    provider_voice_id: str
    provider_model: str | None = None
    source_type: VoiceSourceTypeEnum
    display_name: str
    description: str | None = None
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    status: VoiceStatusEnum
    provider_status: str | None = None
    preview_text: str | None = None
    preview_audio_url: str | None = None
    language_tags: list[str] | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="metadata")
    bound_character_count: int = 0
    bound_character_ids: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class VoiceProfilesPageResponse(SQLModel):
    items: list[VoiceProfileResponse]
    next_cursor: str | None = None
    has_more: bool


class VoiceCatalogResponse(SQLModel):
    items: list[VoiceSelectableItem]


class VoiceCloneAccepted(SQLModel):
    voice: VoiceProfileResponse
    provider_request_id: str | None = None
    estimated_ready_seconds: int | None = None


class VoiceProfileUpdate(SQLModel):
    model_config = {"extra": "forbid"}

    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=160)
    preview_text: str | None = Field(default=None, min_length=1, max_length=120)
    avatar_image_key: str | None = Field(default=None)
    character_ids: list[str] | None = None
