"""Repository for user settings read/write operations."""

from __future__ import annotations

import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.models import UserSettings


class UserSettingsRepository:
    """Data access for `user_settings` table."""

    def __init__(self, db: AsyncSession):
        self._db: AsyncSession = db

    async def get_by_user_id(self, user_id: uuid.UUID) -> UserSettings | None:
        stmt = select(UserSettings).where(UserSettings.user_id == user_id)
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def list_proactive_enabled_users(self) -> list[tuple[uuid.UUID, str]]:
        stmt = select(UserSettings.user_id, UserSettings.timezone).where(
            UserSettings.proactive_enabled.is_(True)
        )
        rows = await self._db.execute(stmt)
        return [(row[0], row[1]) for row in rows.all()]

    async def create_default(
        self,
        user_id: uuid.UUID,
        *,
        default_font_size: int,
        default_display_mode: str,
        default_memory_enabled: bool,
        default_reply_card_enabled: bool,
        default_mixed_input_auto_translate_enabled: bool,
        default_auto_read_aloud_enabled: bool,
        default_preferred_expression_bias_enabled: bool,
        default_proactive_enabled: bool,
        default_timezone: str,
    ) -> UserSettings:
        settings = UserSettings(
            user_id=user_id,
            message_font_size=default_font_size,
            display_mode=default_display_mode,
            memory_enabled=default_memory_enabled,
            reply_card_enabled=default_reply_card_enabled,
            mixed_input_auto_translate_enabled=default_mixed_input_auto_translate_enabled,
            auto_read_aloud_enabled=default_auto_read_aloud_enabled,
            preferred_expression_bias_enabled=default_preferred_expression_bias_enabled,
            proactive_enabled=default_proactive_enabled,
            timezone=default_timezone,
        )
        self._db.add(settings)
        await self._db.flush()
        await self._db.refresh(settings)
        return settings

    async def get_or_create(
        self,
        user_id: uuid.UUID,
        *,
        default_font_size: int,
        default_display_mode: str,
        default_memory_enabled: bool,
        default_reply_card_enabled: bool,
        default_mixed_input_auto_translate_enabled: bool,
        default_auto_read_aloud_enabled: bool,
        default_preferred_expression_bias_enabled: bool,
        default_proactive_enabled: bool,
        default_timezone: str,
    ) -> UserSettings:
        settings = await self.get_by_user_id(user_id)
        if settings:
            return settings
        return await self.create_default(
            user_id,
            default_font_size=default_font_size,
            default_display_mode=default_display_mode,
            default_memory_enabled=default_memory_enabled,
            default_reply_card_enabled=default_reply_card_enabled,
            default_mixed_input_auto_translate_enabled=default_mixed_input_auto_translate_enabled,
            default_auto_read_aloud_enabled=default_auto_read_aloud_enabled,
            default_preferred_expression_bias_enabled=default_preferred_expression_bias_enabled,
            default_proactive_enabled=default_proactive_enabled,
            default_timezone=default_timezone,
        )

    async def update(
        self,
        settings: UserSettings,
        *,
        message_font_size: int | None = None,
        display_mode: str | None = None,
        memory_enabled: bool | None = None,
        reply_card_enabled: bool | None = None,
        mixed_input_auto_translate_enabled: bool | None = None,
        auto_read_aloud_enabled: bool | None = None,
        preferred_expression_bias_enabled: bool | None = None,
        proactive_enabled: bool | None = None,
        timezone: str | None = None,
    ) -> UserSettings:
        if message_font_size is not None:
            settings.message_font_size = message_font_size
        if display_mode is not None:
            settings.display_mode = display_mode
        if memory_enabled is not None:
            settings.memory_enabled = memory_enabled
        if reply_card_enabled is not None:
            settings.reply_card_enabled = reply_card_enabled
        if mixed_input_auto_translate_enabled is not None:
            settings.mixed_input_auto_translate_enabled = mixed_input_auto_translate_enabled
        if auto_read_aloud_enabled is not None:
            settings.auto_read_aloud_enabled = auto_read_aloud_enabled
        if preferred_expression_bias_enabled is not None:
            settings.preferred_expression_bias_enabled = preferred_expression_bias_enabled
        if proactive_enabled is not None:
            settings.proactive_enabled = proactive_enabled
        if timezone is not None:
            settings.timezone = timezone

        await self._db.flush()
        await self._db.refresh(settings)
        return settings
