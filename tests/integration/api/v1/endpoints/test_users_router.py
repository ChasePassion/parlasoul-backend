from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import users
from src.core.dependencies import (
    get_proactive_preference_service,
    get_subscription_service,
    get_user_service,
    get_user_settings_service,
)
from src.db.models import User
from src.schemas.character import CharacterResponse
from src.schemas.chat import CharacterBrief
from src.schemas.proactive import (
    ProactiveCharacterPreferenceItem,
    ProactiveCharacterPreferenceListResponse,
)
from src.schemas.user import (
    UserEntitlementFeatures,
    UserEntitlementSettings,
    UserEntitlementsResponse,
    UserProfileResponse,
)
from src.schemas.user_settings import UserSettingsResponse


class _StubUserService:
    def __init__(self) -> None:
        self.get_my_profile = AsyncMock()
        self.update_my_profile = AsyncMock()
        self.update_onboarding_status = AsyncMock()
        self.get_my_settings = AsyncMock()
        self.update_my_settings = AsyncMock()
        self.get_creator_characters = AsyncMock()


class _StubSubscriptionService:
    def __init__(self) -> None:
        self.build_entitlements_payload = AsyncMock()


class _StubProactivePreferenceService:
    def __init__(self) -> None:
        self.list_my_preferences = AsyncMock()
        self.replace_enabled_character_ids = AsyncMock()


def _build_user() -> User:
    return User(id=uuid.uuid4(), email="viewer@example.com")


def _create_app(
    service: _StubUserService,
    subscription_service: _StubSubscriptionService | None = None,
    proactive_service: _StubProactivePreferenceService | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(users.router)

    async def _override_current_user() -> User:
        return _build_user()

    async def _override_optional_user() -> User | None:
        return _build_user()

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[deps.get_optional_current_user] = _override_optional_user
    app.dependency_overrides[get_user_service] = lambda: service
    app.dependency_overrides[get_user_settings_service] = lambda: service
    app.dependency_overrides[get_subscription_service] = lambda: (
        subscription_service or _StubSubscriptionService()
    )
    app.dependency_overrides[get_proactive_preference_service] = lambda: (
        proactive_service or _StubProactivePreferenceService()
    )
    return app


def test_get_my_profile_delegates_to_service() -> None:
    service = _StubUserService()
    service.get_my_profile.return_value = UserProfileResponse(
        id=str(uuid.uuid4()),
        email="viewer@example.com",
        username="viewer",
        email_verified=True,
        avatar_image_key=None,
        avatar_urls=None,
        created_at="2026-01-01T00:00:00Z",
        last_login_at=None,
        onboarding_status="completed",
    )
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.get("/v1/users/me")

    assert resp.status_code == 200
    assert resp.json()["code"] == "ok"
    service.get_my_profile.assert_awaited_once()


def test_update_my_profile_delegates_to_service() -> None:
    service = _StubUserService()
    service.update_my_profile.return_value = UserProfileResponse(
        id=str(uuid.uuid4()),
        email="viewer@example.com",
        username="updated",
        email_verified=True,
        avatar_image_key="images/avatars/users/user-1/image-1",
        avatar_urls=None,
        created_at="2026-01-01T00:00:00Z",
        last_login_at=None,
        onboarding_status="completed",
    )
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.put("/v1/users/me", json={"username": "updated"})

    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "updated"
    service.update_my_profile.assert_awaited_once()


def test_update_my_onboarding_status_delegates_to_service() -> None:
    service = _StubUserService()
    service.update_onboarding_status.return_value = UserProfileResponse(
        id=str(uuid.uuid4()),
        email="viewer@example.com",
        username="viewer",
        email_verified=True,
        avatar_image_key=None,
        avatar_urls=None,
        created_at="2026-01-01T00:00:00Z",
        last_login_at=None,
        onboarding_status="discover_done",
    )
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.patch(
            "/v1/users/me/onboarding",
            json={"status": "discover_done"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["onboarding_status"] == "discover_done"
    service.update_onboarding_status.assert_awaited_once()


def test_get_my_settings_delegates_to_service() -> None:
    service = _StubUserService()
    service.get_my_settings.return_value = UserSettingsResponse(
        display_mode="concise",
        memory_enabled=True,
        reply_card_enabled=True,
        mixed_input_auto_translate_enabled=True,
        auto_read_aloud_enabled=True,
        preferred_expression_bias_enabled=True,
        proactive_enabled=False,
        timezone="Asia/Shanghai",
        message_font_size=16,
        updated_at=datetime.now(UTC),
    )
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.get("/v1/users/me/settings")

    assert resp.status_code == 200
    assert resp.json()["data"]["message_font_size"] == 16
    service.get_my_settings.assert_awaited_once()


def test_update_my_settings_delegates_to_service() -> None:
    service = _StubUserService()
    service.update_my_settings.return_value = UserSettingsResponse(
        display_mode="detailed",
        memory_enabled=True,
        reply_card_enabled=True,
        mixed_input_auto_translate_enabled=False,
        auto_read_aloud_enabled=False,
        preferred_expression_bias_enabled=False,
        proactive_enabled=False,
        timezone="Asia/Shanghai",
        message_font_size=18,
        updated_at=datetime.now(UTC),
    )
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.patch("/v1/users/me/settings", json={"message_font_size": 18})

    assert resp.status_code == 200
    assert resp.json()["data"]["message_font_size"] == 18
    service.update_my_settings.assert_awaited_once()


def test_get_my_entitlements_delegates_to_subscription_service() -> None:
    service = _StubUserService()
    subscription_service = _StubSubscriptionService()
    service.get_my_settings.return_value = UserSettingsResponse(
        display_mode="concise",
        memory_enabled=True,
        reply_card_enabled=True,
        mixed_input_auto_translate_enabled=True,
        auto_read_aloud_enabled=True,
        preferred_expression_bias_enabled=True,
        proactive_enabled=False,
        timezone="Asia/Shanghai",
        message_font_size=16,
        updated_at=datetime.now(UTC),
    )
    subscription_service.build_entitlements_payload.return_value = UserEntitlementsResponse(
        tier="plus",
        subscription_status="active",
        subscription_product_id="prod_plus",
        current_period_end=datetime.now(UTC),
        effective_source="recurring_subscription",
        effective_expires_at=datetime.now(UTC),
        active_pass=None,
        features=UserEntitlementFeatures(voice_clone=True, memory_feature=True),
        settings=UserEntitlementSettings(memory_enabled=True),
    )
    app = _create_app(service, subscription_service)

    with TestClient(app) as client:
        resp = client.get("/v1/users/me/entitlements")

    assert resp.status_code == 200
    assert resp.json()["data"]["tier"] == "plus"
    assert resp.json()["data"]["subscription_status"] == "active"
    assert resp.json()["data"]["subscription_product_id"] == "prod_plus"
    service.get_my_settings.assert_awaited_once()
    subscription_service.build_entitlements_payload.assert_awaited_once()


def test_get_my_proactive_characters_delegates_to_service() -> None:
    service = _StubUserService()
    proactive_service = _StubProactivePreferenceService()
    proactive_service.list_my_preferences.return_value = ProactiveCharacterPreferenceListResponse(
        items=[
            ProactiveCharacterPreferenceItem(
                character=CharacterBrief(
                    id=str(uuid.uuid4()),
                    name="Luna",
                    description="desc",
                    greeting_message="hello",
                    avatar_image_key=None,
                    avatar_urls=None,
                    status="ACTIVE",
                    unpublished_at=None,
                    visibility="PUBLIC",
                    creator_id=None,
                    creator_username=None,
                ),
                enabled=True,
                last_interaction_at=datetime.now(UTC),
            )
        ]
    )
    app = _create_app(service, proactive_service=proactive_service)

    with TestClient(app) as client:
        resp = client.get("/v1/users/me/proactive-characters")

    assert resp.status_code == 200
    assert resp.json()["data"]["items"][0]["enabled"] is True
    proactive_service.list_my_preferences.assert_awaited_once()


def test_replace_my_proactive_characters_delegates_to_service() -> None:
    service = _StubUserService()
    proactive_service = _StubProactivePreferenceService()
    character_id = str(uuid.uuid4())
    proactive_service.replace_enabled_character_ids.return_value = ProactiveCharacterPreferenceListResponse(
        items=[
            ProactiveCharacterPreferenceItem(
                character=CharacterBrief(
                    id=character_id,
                    name="Luna",
                    description="desc",
                    greeting_message="hello",
                    avatar_image_key=None,
                    avatar_urls=None,
                    status="ACTIVE",
                    unpublished_at=None,
                    visibility="PUBLIC",
                    creator_id=None,
                    creator_username=None,
                ),
                enabled=True,
                last_interaction_at=datetime.now(UTC),
            )
        ]
    )
    app = _create_app(service, proactive_service=proactive_service)

    with TestClient(app) as client:
        resp = client.put(
            "/v1/users/me/proactive-characters",
            json={"character_ids": [character_id]},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["items"][0]["character"]["id"] == character_id
    proactive_service.replace_enabled_character_ids.assert_awaited_once()


def test_get_creator_characters_delegates_to_service() -> None:
    service = _StubUserService()
    service.get_creator_characters.return_value = [
        CharacterResponse(
            id=str(uuid.uuid4()),
            name="Mika",
            description="desc",
            system_prompt="prompt",
            greeting_message="hello",
            avatar_image_key=None,
            avatar_urls=None,
            voice_provider="dashscope",
            voice_model="qwen3-tts-instruct-flash-realtime",
            voice_provider_voice_id="Cherry",
            voice_source_type="system",
            creator_id=str(uuid.uuid4()),
            creator_username="creator",
            visibility="PUBLIC",
            identifier=None,
            interaction_count=0,
        )
    ]
    app = _create_app(service)
    creator_id = str(uuid.uuid4())

    with TestClient(app) as client:
        resp = client.get(f"/v1/users/{creator_id}/characters")

    assert resp.status_code == 200
    assert resp.json()["status"] == 200
    service.get_creator_characters.assert_awaited_once()
