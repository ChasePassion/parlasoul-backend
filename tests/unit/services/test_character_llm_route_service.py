from __future__ import annotations

import uuid

import pytest

from src.core.dialogue_styles import LLM_PRESET_FLAGSHIP
from src.core.exceptions import APIError
from src.core.llm_registry import LLMRegistry, LLMRouter
from src.core.llm_settings import llm_settings
from src.db.models import Character
from src.services.character_llm_route_service import CharacterLLMRouteService


def _clear_llm_setting_cache() -> None:
    for key in (
        "deepseek_api_key",
        "deepseek_api_base",
        "deepseek_model_id",
        "openrouter_api_key",
        "openrouter_api_base",
        "openrouter_model_id",
        "openai_api_key",
        "openai_api_base",
        "openai_model_id",
        "anthropic_api_key",
        "anthropic_api_base",
        "anthropic_model_id",
    ):
        llm_settings.__dict__.pop(key, None)


def _build_registry(*, chat_provider: str | None = None) -> LLMRegistry:
    registry = LLMRegistry(llm_settings)
    registry._router = LLMRouter(
        chat_provider=chat_provider or registry.router.chat_provider,
        title_provider=registry.router.title_provider,
        learning_provider=registry.router.learning_provider,
        memory_provider=registry.router.memory_provider,
    )
    return registry


def _build_character() -> Character:
    return Character(
        id=uuid.uuid4(),
        name="Mika",
        description="desc",
        system_prompt="prompt",
        greeting_message="hi",
        voice_provider="dashscope",
        voice_model="qwen3-tts-instruct-flash-realtime",
        voice_provider_voice_id="Cherry",
        voice_source_type="system",
        visibility="PRIVATE",
        interaction_count=0,
    )


@pytest.mark.asyncio
async def test_resolve_runtime_route_raises_when_default_route_missing_credentials(monkeypatch) -> None:
    monkeypatch.setattr(llm_settings, "LLM_DEEPSEEK_API_KEY", None, raising=False)
    monkeypatch.setattr(llm_settings, "LLM_DEEPSEEK_MODEL_ID", "deepseek-chat", raising=False)
    _clear_llm_setting_cache()

    registry = _build_registry(chat_provider="deepseek")
    character = _build_character()
    service = CharacterLLMRouteService(registry=registry)

    with pytest.raises(APIError) as exc:
        await service.resolve_runtime_route(character)

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_resolve_runtime_route_uses_default_route_for_free_preset(monkeypatch) -> None:
    monkeypatch.setattr(llm_settings, "LLM_OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setattr(
        llm_settings,
        "LLM_OPENROUTER_MODEL_ID",
        "openai/gpt-4.1-mini",
        raising=False,
    )
    _clear_llm_setting_cache()

    character = _build_character()
    service = CharacterLLMRouteService(registry=_build_registry(chat_provider="openrouter"))

    route = await service.resolve_runtime_route(character)

    assert route.provider == "openrouter"
    assert route.model == "openai/gpt-4.1-mini"
    assert route.source == "system_chat"


@pytest.mark.asyncio
async def test_resolve_runtime_route_uses_default_route_for_flagship_preset(monkeypatch) -> None:
    monkeypatch.setattr(llm_settings, "LLM_DEEPSEEK_API_KEY", "deepseek-key", raising=False)
    monkeypatch.setattr(
        llm_settings,
        "LLM_DEEPSEEK_MODEL_ID",
        "deepseek-v4.1-flash",
        raising=False,
    )
    _clear_llm_setting_cache()

    character = _build_character()
    character.llm_preset_id = LLM_PRESET_FLAGSHIP
    service = CharacterLLMRouteService(registry=_build_registry())

    route = await service.resolve_runtime_route(character)
    default_route = service.default_route()

    assert route.provider == "deepseek"
    assert route.model == "deepseek-v4.1-flash"
    assert route.sdk_kind == "openai_sdk"
    assert route.source == "system_chat"
    assert route.api_key == "deepseek-key"
    assert (route.provider, route.model, route.source) == (
        default_route.provider,
        default_route.model,
        default_route.source,
    )
