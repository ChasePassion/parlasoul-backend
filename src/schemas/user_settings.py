"""Schemas for per-user chat UI settings."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import field_validator
from sqlmodel import Field, SQLModel

from src.core.config import settings

DisplayMode = Literal["concise", "detailed"]


class UserSettingsResponse(SQLModel):
    display_mode: DisplayMode = Field(default=settings.USER_SETTINGS_DISPLAY_MODE_DEFAULT)
    memory_enabled: bool = Field(default=True)
    reply_card_enabled: bool = Field(default=settings.USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT)
    mixed_input_auto_translate_enabled: bool = Field(
        default=settings.USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT
    )
    auto_read_aloud_enabled: bool = Field(
        default=settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT
    )
    preferred_expression_bias_enabled: bool = Field(
        default=settings.USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT
    )
    proactive_enabled: bool = Field(default=False)
    timezone: str = Field(default="Asia/Shanghai")
    message_font_size: int = Field(
        ge=settings.USER_SETTINGS_MESSAGE_FONT_SIZE_MIN,
        le=settings.USER_SETTINGS_MESSAGE_FONT_SIZE_MAX,
    )
    updated_at: datetime
    model_config = {"from_attributes": True}


class UserSettingsUpdate(SQLModel):
    display_mode: DisplayMode | None = None
    memory_enabled: bool | None = None
    reply_card_enabled: bool | None = None
    mixed_input_auto_translate_enabled: bool | None = None
    auto_read_aloud_enabled: bool | None = None
    preferred_expression_bias_enabled: bool | None = None
    proactive_enabled: bool | None = None
    timezone: str | None = None
    message_font_size: int | None = Field(
        default=None,
        ge=settings.USER_SETTINGS_MESSAGE_FONT_SIZE_MIN,
        le=settings.USER_SETTINGS_MESSAGE_FONT_SIZE_MAX,
    )
    model_config = {"extra": "forbid"}

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        ZoneInfo(value)
        return value
