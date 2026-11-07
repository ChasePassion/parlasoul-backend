"""Character schemas for request/response models."""

from datetime import datetime
from enum import StrEnum

from pydantic import field_validator
from sqlmodel import Field, SQLModel

from src.core.llm_presets import LLM_PRESET_FREE, LLM_PRESET_IDS
from src.core.tts_registry import (
    DEFAULT_SYSTEM_TTS_MODEL,
    DEFAULT_SYSTEM_TTS_PROVIDER,
    DEFAULT_SYSTEM_TTS_VOICE_ID,
)
from src.schemas.media import AvatarUrls
from src.schemas.voice_profile import VoiceSelectableItem, VoiceSourceTypeEnum


class VisibilityEnum(StrEnum):
    """Visibility enum for API."""
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class CharacterStatusEnum(StrEnum):
    """Lifecycle status for character resources."""

    ACTIVE = "ACTIVE"
    UNPUBLISHED = "UNPUBLISHED"


def _normalize_preset_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("llm_preset_id must be a string")
    normalized = value.strip().lower()
    if normalized not in LLM_PRESET_IDS:
        raise ValueError("llm_preset_id must be one of: free, flagship")
    return normalized


class CharacterCreate(SQLModel):
    """Request model for creating a character."""
    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=100, description="Character name")
    description: str = Field(..., min_length=1, max_length=35, description="Brief character description for card display")
    system_prompt: str = Field(..., min_length=1, description="System prompt for the character")
    greeting_message: str | None = Field(None, description="Character's greeting message (chat opening)")
    avatar_image_key: str | None = Field(None, description="R2 avatar image key from /v1/uploads/complete")
    voice_provider: str = Field(
        default=DEFAULT_SYSTEM_TTS_PROVIDER,
        min_length=1,
        max_length=40,
        description="Voice provider",
    )
    voice_model: str = Field(
        default=DEFAULT_SYSTEM_TTS_MODEL,
        min_length=1,
        max_length=120,
        description="Provider model name used for TTS",
    )
    voice_provider_voice_id: str = Field(
        default=DEFAULT_SYSTEM_TTS_VOICE_ID,
        min_length=1,
        max_length=191,
        description="Provider voice id/code",
    )
    voice_source_type: VoiceSourceTypeEnum = Field(
        default=VoiceSourceTypeEnum.SYSTEM,
        description="Voice source type",
    )
    llm_preset_id: str = Field(
        default=LLM_PRESET_FREE,
        max_length=40,
        description="LLM preset tier: 'free' or 'flagship'.",
    )
    visibility: VisibilityEnum = Field(VisibilityEnum.PRIVATE, description="Character visibility: PUBLIC or PRIVATE")

    @field_validator("llm_preset_id", mode="before")
    @classmethod
    def validate_llm_preset_id(cls, value: str | None) -> str | None:
        return _normalize_preset_id(value)


class CharacterUpdate(SQLModel):
    """Request model for updating a character. All fields are optional."""
    model_config = {"extra": "forbid"}

    name: str | None = Field(None, min_length=1, max_length=100, description="Character name")
    description: str | None = Field(None, min_length=1, max_length=35, description="Brief character description")
    system_prompt: str | None = Field(None, min_length=1, description="System prompt")
    greeting_message: str | None = Field(None, description="Greeting message")
    avatar_image_key: str | None = Field(None, description="R2 avatar image key")
    voice_provider: str | None = Field(
        None,
        min_length=1,
        max_length=40,
        description="Voice provider",
    )
    voice_model: str | None = Field(
        None,
        min_length=1,
        max_length=120,
        description="Provider model name used for TTS",
    )
    voice_provider_voice_id: str | None = Field(
        None,
        min_length=1,
        max_length=191,
        description="Provider voice id/code",
    )
    voice_source_type: VoiceSourceTypeEnum | None = Field(
        None,
        description="Voice source type",
    )
    llm_preset_id: str | None = Field(
        default=None,
        max_length=40,
        description="LLM preset tier. Omit to keep unchanged.",
    )
    visibility: VisibilityEnum | None = Field(None, description="Update visibility status")

    @field_validator("llm_preset_id", mode="before")
    @classmethod
    def validate_llm_preset_id(cls, value: str | None) -> str | None:
        return _normalize_preset_id(value)


class CharacterResponse(SQLModel):
    """Response model for character data."""
    id: str
    name: str
    description: str
    system_prompt: str
    greeting_message: str | None = None
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    voice_provider: str
    voice_model: str
    voice_provider_voice_id: str
    voice_source_type: VoiceSourceTypeEnum
    voice: VoiceSelectableItem | None = None
    llm_preset_id: str | None = None
    creator_id: str | None = None
    creator_username: str | None = None
    status: CharacterStatusEnum = CharacterStatusEnum.ACTIVE
    unpublished_at: datetime | None = None
    visibility: str  # Return as string for flexibility
    identifier: str | None = None
    distinct_user_count: int = 0
