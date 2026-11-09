"""Media upload and avatar URL schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel


class MediaUploadKind(StrEnum):
    USER_AVATAR = "user_avatar"
    CHARACTER_AVATAR = "character_avatar"
    VOICE_AVATAR = "voice_avatar"
    CHAT_IMAGE = "chat_image"


class AvatarUrls(SQLModel):
    sm: str
    md: str
    lg: str
    xl: str


class ChatAudioUploadResponse(SQLModel):
    audio_key: str
    duration_seconds: float
    size_bytes: int
    mime_type: str
    name: str


class ChatImageUploadResponse(SQLModel):
    image_key: str
    original_url: str
    display_url: str
    content_type: str
    size_bytes: int
    width: int
    height: int


class PresignedUploadRequest(SQLModel):
    model_config = ConfigDict(extra="forbid")

    kind: MediaUploadKind
    mime_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)


class PresignedUploadResponse(SQLModel):
    upload_id: str
    image_key: str
    original_object_key: str
    upload_url: str
    expires_in_seconds: int
    required_headers: dict[str, str]


class CompleteUploadRequest(SQLModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str = Field(min_length=1)
    etag: str | None = None
    object_key: str | None = None
    image_key: str | None = None


class CompleteUploadResponse(SQLModel):
    upload_id: str
    image_key: str
    avatar_urls: AvatarUrls | None = None
    content_type: str = "image/avif"
    original_url: str | None = None
    display_url: str | None = None
