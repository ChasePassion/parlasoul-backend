"""Service layer for user settings."""

from __future__ import annotations

import uuid
from typing import cast

from src.core.config import settings as app_settings
from src.core.exceptions import APIError
from src.core.uow import UnitOfWork
from src.db.models import User, UserSettings
from src.repositories.user_settings_repository import UserSettingsRepository
from src.schemas.user_settings import DisplayMode, UserSettingsResponse, UserSettingsUpdate
from src.services.subscription_service import SubscriptionService


def _to_settings_response(settings: UserSettings) -> UserSettingsResponse:
    if settings.updated_at is None:
        raise APIError(
            status_code=500,
            code="user_settings_updated_at_missing",
            message="server state invalid: updated_at is missing for user settings",
        )

    return UserSettingsResponse(
        display_mode=cast(DisplayMode, settings.display_mode),
        memory_enabled=settings.memory_enabled,
        reply_card_enabled=settings.reply_card_enabled,
        mixed_input_auto_translate_enabled=settings.mixed_input_auto_translate_enabled,
        auto_read_aloud_enabled=settings.auto_read_aloud_enabled,
        preferred_expression_bias_enabled=settings.preferred_expression_bias_enabled,
        proactive_enabled=settings.proactive_enabled,
        timezone=settings.timezone,
        message_font_size=settings.message_font_size,
        updated_at=settings.updated_at,
    )


class UserSettingsService:
    """Business logic for user settings CRUD."""

    def __init__(
        self,
        settings_repo: UserSettingsRepository,
        uow: UnitOfWork,
        subscription_service: SubscriptionService,
    ):
        self._settings_repo: UserSettingsRepository = settings_repo
        self._uow: UnitOfWork = uow
        self._subscription_service: SubscriptionService = subscription_service

    async def _get_or_create_settings(self, user_id: uuid.UUID) -> UserSettings:
        return await self._settings_repo.get_or_create(
            user_id,
            default_font_size=app_settings.USER_SETTINGS_MESSAGE_FONT_SIZE_DEFAULT,
            default_display_mode=app_settings.USER_SETTINGS_DISPLAY_MODE_DEFAULT,
            default_memory_enabled=True,
            default_reply_card_enabled=app_settings.USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT,
            default_mixed_input_auto_translate_enabled=(
                app_settings.USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT
            ),
            default_auto_read_aloud_enabled=(
                app_settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT
            ),
            default_preferred_expression_bias_enabled=(
                app_settings.USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT
            ),
            default_proactive_enabled=app_settings.USER_SETTINGS_PROACTIVE_ENABLED_DEFAULT,
            default_timezone=app_settings.USER_SETTINGS_TIMEZONE_DEFAULT,
        )

    async def get_my_settings(self, current_user: User) -> UserSettingsResponse:
        async with self._uow.transactional():
            settings = await self._get_or_create_settings(current_user.id)
        return _to_settings_response(settings)

    async def update_my_settings(
        self,
        settings_update: UserSettingsUpdate,
        *,
        current_user: User,
    ) -> UserSettingsResponse:
        if (
            settings_update.message_font_size is None
            and settings_update.display_mode is None
            and settings_update.memory_enabled is None
            and settings_update.reply_card_enabled is None
            and settings_update.mixed_input_auto_translate_enabled is None
            and settings_update.auto_read_aloud_enabled is None
            and settings_update.preferred_expression_bias_enabled is None
            and settings_update.proactive_enabled is None
            and settings_update.timezone is None
        ):
            raise APIError(
                status_code=400,
                code="user_settings_update_empty",
                message=(
                    "invalid request: at least one field is required; "
                    "fields=display_mode,memory_enabled,reply_card_enabled,mixed_input_auto_translate_enabled,"
                    "auto_read_aloud_enabled,preferred_expression_bias_enabled,proactive_enabled,timezone,"
                    "message_font_size"
                ),
            )

        if settings_update.memory_enabled is not None:
            await self._subscription_service.assert_feature(current_user, "memory_feature")

        async with self._uow.transactional():
            settings = await self._get_or_create_settings(current_user.id)
            updated = await self._settings_repo.update(
                settings,
                message_font_size=settings_update.message_font_size,
                display_mode=settings_update.display_mode,
                memory_enabled=settings_update.memory_enabled,
                reply_card_enabled=settings_update.reply_card_enabled,
                mixed_input_auto_translate_enabled=settings_update.mixed_input_auto_translate_enabled,
                auto_read_aloud_enabled=settings_update.auto_read_aloud_enabled,
                preferred_expression_bias_enabled=settings_update.preferred_expression_bias_enabled,
                proactive_enabled=settings_update.proactive_enabled,
                timezone=settings_update.timezone,
            )
        return _to_settings_response(updated)
