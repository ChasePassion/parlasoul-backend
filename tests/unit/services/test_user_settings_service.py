from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.config import settings as app_settings
from src.core.exceptions import APIError
from src.db.models import User, UserSettings
from src.repositories.user_settings_repository import UserSettingsRepository
from src.schemas.user_settings import UserSettingsUpdate
from src.services.user_settings_service import UserSettingsService


def _build_user(user_id: uuid.UUID | None = None) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        email="user@example.com",
        username="tester",
        avatar_image_key=None,
        created_at=datetime.now(UTC),
    )


def _build_user_settings(user_id: uuid.UUID, message_font_size: int = 16) -> UserSettings:
    return UserSettings(
        user_id=user_id,
        message_font_size=message_font_size,
        display_mode="concise",
        memory_enabled=True,
        reply_card_enabled=True,
        mixed_input_auto_translate_enabled=True,
        auto_read_aloud_enabled=True,
        preferred_expression_bias_enabled=True,
        proactive_enabled=False,
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class _StubUow:
    @asynccontextmanager
    async def transactional(self):
        yield


def _build_subscription_service() -> SimpleNamespace:
    return SimpleNamespace(assert_feature=AsyncMock())


@pytest.mark.asyncio
async def test_get_my_settings_returns_repository_value() -> None:
    user = _build_user()
    repo = UserSettingsRepository.__new__(UserSettingsRepository)
    repo.get_or_create = AsyncMock(return_value=_build_user_settings(user.id, 16))
    service = UserSettingsService(repo, _StubUow(), _build_subscription_service())

    result = await service.get_my_settings(current_user=user)

    assert result.message_font_size == 16
    repo.get_or_create.assert_awaited_once_with(
        user.id,
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


@pytest.mark.asyncio
async def test_update_my_settings_empty_payload_raises() -> None:
    user = _build_user()
    repo = UserSettingsRepository.__new__(UserSettingsRepository)
    repo.get_or_create = AsyncMock()
    repo.update = AsyncMock()
    subscription_service = _build_subscription_service()
    service = UserSettingsService(repo, _StubUow(), subscription_service)

    with pytest.raises(APIError) as exc:
        await service.update_my_settings(UserSettingsUpdate(), current_user=user)

    assert exc.value.status_code == 400
    assert exc.value.code == "user_settings_update_empty"
    repo.get_or_create.assert_not_called()
    repo.update.assert_not_called()
    subscription_service.assert_feature.assert_not_called()


@pytest.mark.asyncio
async def test_update_my_settings_delegates_to_repository() -> None:
    user = _build_user()
    existing = _build_user_settings(user.id, 16)
    updated = _build_user_settings(user.id, 18)

    repo = UserSettingsRepository.__new__(UserSettingsRepository)
    repo.get_or_create = AsyncMock(return_value=existing)
    repo.update = AsyncMock(return_value=updated)
    subscription_service = _build_subscription_service()
    service = UserSettingsService(repo, _StubUow(), subscription_service)

    result = await service.update_my_settings(
        UserSettingsUpdate(message_font_size=18),
        current_user=user,
    )

    assert result.message_font_size == 18
    repo.get_or_create.assert_awaited_once_with(
        user.id,
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
    repo.update.assert_awaited_once_with(
        existing,
        message_font_size=18,
        display_mode=None,
        memory_enabled=None,
        reply_card_enabled=None,
        mixed_input_auto_translate_enabled=None,
        auto_read_aloud_enabled=None,
        preferred_expression_bias_enabled=None,
        proactive_enabled=None,
        timezone=None,
    )
    subscription_service.assert_feature.assert_not_called()


@pytest.mark.asyncio
async def test_update_my_settings_requires_subscription_for_memory_toggle() -> None:
    user = _build_user()
    repo = UserSettingsRepository.__new__(UserSettingsRepository)
    repo.get_or_create = AsyncMock()
    repo.update = AsyncMock()
    subscription_service = _build_subscription_service()
    subscription_service.assert_feature.side_effect = APIError(
        status_code=403,
        code="subscription_required",
        message="subscription required",
    )
    service = UserSettingsService(repo, _StubUow(), subscription_service)

    with pytest.raises(APIError) as exc:
        await service.update_my_settings(
            UserSettingsUpdate(memory_enabled=False),
            current_user=user,
        )

    assert exc.value.code == "subscription_required"
    subscription_service.assert_feature.assert_awaited_once_with(user, "memory_feature")
    repo.get_or_create.assert_not_called()
    repo.update.assert_not_called()
