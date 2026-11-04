"""Centralized LLM provider registry and route binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.core.llm_settings import LLMSettings, llm_settings

LLMSDKKind = Literal["openai_sdk", "pydantic_ai", "anthropic_sdk"]

_DEEPSEEK_SELECTABLE_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
_DEFAULT_MODEL_ALIAS = "default"


@dataclass(frozen=True)
class ProviderProfile:
    provider: str
    sdk_kind: LLMSDKKind
    api_key: str | None
    api_base: str | None
    model_id: str
    model_aliases: dict[str, str]
    selectable_models: tuple[str, ...]
    enabled: bool
    supports_stream: bool
    supports_structured_output: bool
    default_model_alias: str = _DEFAULT_MODEL_ALIAS
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None

    def model_for_alias(self, alias: str | None) -> str | None:
        alias_key = (alias or self.default_model_alias).strip().lower()
        return self.model_aliases.get(alias_key)


@dataclass(frozen=True)
class ResolvedLLMRoute:
    provider: str
    model: str
    sdk_kind: LLMSDKKind
    api_key: str | None
    api_base: str | None
    source: str
    model_type_label: str
    model_alias: str | None = None
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMRouter:
    chat_provider: str
    title_provider: str
    learning_provider: str
    memory_provider: str


class LLMRegistry:
    """Single source of truth for provider routing and provider-local config."""

    def __init__(self, settings: LLMSettings):
        self._settings = settings
        self._router = LLMRouter(
            chat_provider="deepseek",
            title_provider="dashscope:title",
            learning_provider="dashscope:learning",
            memory_provider="dashscope:memory",
        )

    @property
    def router(self) -> LLMRouter:
        return self._router

    def _openai_profile(self) -> ProviderProfile:
        model_id = self._settings.openai_model_id or "gpt-5.1-mini"
        api_key = self._settings.openai_api_key
        return ProviderProfile(
            provider="openai",
            sdk_kind="openai_sdk",
            api_key=api_key,
            api_base=self._settings.openai_api_base,
            model_id=model_id,
            model_aliases={_DEFAULT_MODEL_ALIAS: model_id},
            selectable_models=(model_id,),
            enabled=bool(api_key and model_id),
            supports_stream=True,
            supports_structured_output=True,
        )

    def _deepseek_profile(self) -> ProviderProfile:
        model_id = self._settings.deepseek_model_id or "deepseek-v4-flash"
        return ProviderProfile(
            provider="deepseek",
            sdk_kind="openai_sdk",
            api_key=self._settings.deepseek_api_key,
            api_base=self._settings.deepseek_api_base,
            model_id=model_id,
            model_aliases={_DEFAULT_MODEL_ALIAS: model_id},
            selectable_models=_DEEPSEEK_SELECTABLE_MODELS,
            enabled=bool(self._settings.deepseek_api_key and model_id),
            supports_stream=True,
            supports_structured_output=True,
            extra_headers=self._settings.deepseek_extra_headers,
            extra_body=self._settings.deepseek_extra_body,
        )

    def _dashscope_profile(self) -> ProviderProfile:
        model_id = self._settings.dashscope_model_id or "qwen3.7-flash"
        model_aliases = {
            _DEFAULT_MODEL_ALIAS: model_id,
            "chat": self._settings.dashscope_chat_model_id or model_id,
            "title": self._settings.dashscope_title_model_id or model_id,
            "memory": self._settings.dashscope_memory_model_id or model_id,
            "learning": self._settings.dashscope_learning_model_id or model_id,
            "vision": self._settings.dashscope_vision_model_id or "qwen3.5-flash",
        }
        return ProviderProfile(
            provider="dashscope",
            sdk_kind="openai_sdk",
            api_key=self._settings.dashscope_api_key,
            api_base=self._settings.dashscope_api_base,
            model_id=model_id,
            model_aliases=model_aliases,
            selectable_models=tuple(dict.fromkeys(model_aliases.values())),
            enabled=bool(self._settings.dashscope_api_key and model_id),
            supports_stream=True,
            supports_structured_output=True,
        )

    def _openrouter_profile(self) -> ProviderProfile:
        model_id = self._settings.openrouter_model_id or "openai/gpt-4.1-mini"
        return ProviderProfile(
            provider="openrouter",
            sdk_kind="pydantic_ai",
            api_key=self._settings.openrouter_api_key,
            api_base=self._settings.openrouter_api_base,
            model_id=model_id,
            model_aliases={_DEFAULT_MODEL_ALIAS: model_id},
            selectable_models=(model_id,),
            enabled=bool(self._settings.openrouter_api_key and model_id),
            supports_stream=True,
            supports_structured_output=True,
        )

    def _anthropic_profile(self) -> ProviderProfile:
        model_id = self._settings.anthropic_model_id or "MiniMax-M2.5-highspeed"
        return ProviderProfile(
            provider="anthropic",
            sdk_kind="anthropic_sdk",
            api_key=self._settings.anthropic_api_key,
            api_base=self._settings.anthropic_api_base,
            model_id=model_id,
            model_aliases={_DEFAULT_MODEL_ALIAS: model_id},
            selectable_models=(
                model_id,
                "MiniMax-M2.7",
                "MiniMax-M2.5",
                "MiniMax-M2.5-highspeed",
                "MiniMax-M2.1",
                "MiniMax-M2.1-highspeed",
                "MiniMax-M2",
            ),
            enabled=bool(self._settings.anthropic_api_key and model_id),
            supports_stream=True,
            supports_structured_output=False,
        )

    def _profiles(self) -> dict[str, ProviderProfile]:
        return {
            "openai": self._openai_profile(),
            "deepseek": self._deepseek_profile(),
            "dashscope": self._dashscope_profile(),
            "openrouter": self._openrouter_profile(),
            "anthropic": self._anthropic_profile(),
        }

    @staticmethod
    def _split_provider_ref(provider_ref: str) -> tuple[str, str | None]:
        normalized_ref = (provider_ref or "").strip().lower()
        if ":" not in normalized_ref:
            return normalized_ref, None
        provider_name, model_alias = normalized_ref.split(":", 1)
        return provider_name.strip(), model_alias.strip() or None

    def get_profile(self, provider: str) -> ProviderProfile | None:
        provider_name, _ = self._split_provider_ref(provider)
        return self._profiles().get(provider_name)

    def enabled_profiles(self) -> list[ProviderProfile]:
        return [profile for profile in self._profiles().values() if profile.enabled]

    def provider_enabled(self, provider: str) -> bool:
        profile = self.get_profile(provider)
        return bool(profile and profile.enabled)

    def selectable_models(self, provider: str) -> list[str]:
        profile = self.get_profile(provider)
        if not profile:
            return []
        return list(profile.selectable_models)

    def resolve_bound_route(self, provider: str, *, source: str) -> ResolvedLLMRoute | None:
        provider_name, model_alias = self._split_provider_ref(provider)
        profile = self.get_profile(provider_name)
        if not profile or not profile.enabled:
            return None
        model_id = profile.model_for_alias(model_alias)
        if not model_id:
            return None
        model_type_label = (
            f"{profile.provider}:{model_alias}:{model_id}"
            if model_alias
            else f"{profile.provider}:{model_id}"
        )
        return ResolvedLLMRoute(
            provider=profile.provider,
            model=model_id,
            sdk_kind=profile.sdk_kind,
            api_key=profile.api_key,
            api_base=profile.api_base,
            source=source,
            model_type_label=model_type_label,
            model_alias=model_alias,
            extra_headers=profile.extra_headers,
            extra_body=profile.extra_body,
        )

    def resolve_chat_route(self) -> ResolvedLLMRoute | None:
        return self.resolve_bound_route(self.router.chat_provider, source="system_chat")

    def resolve_title_route(self) -> ResolvedLLMRoute | None:
        return self.resolve_bound_route(self.router.title_provider, source="system_title")

    def resolve_memory_route(self) -> ResolvedLLMRoute | None:
        return self.resolve_bound_route(self.router.memory_provider, source="system_memory")

    def resolve_learning_route(self) -> ResolvedLLMRoute | None:
        return self.resolve_bound_route(self.router.learning_provider, source="system_learning")

    def resolve_explicit_route(
        self,
        *,
        provider: str,
        model: str,
        source: str,
    ) -> ResolvedLLMRoute | None:
        profile = self.get_profile(provider)
        normalized_model = (model or "").strip()
        if not profile or not profile.enabled or not normalized_model:
            return None
        return ResolvedLLMRoute(
            provider=profile.provider,
            model=normalized_model,
            sdk_kind=profile.sdk_kind,
            api_key=profile.api_key,
            api_base=profile.api_base,
            source=source,
            model_type_label=f"{profile.provider}:{normalized_model}",
            extra_headers=profile.extra_headers,
            extra_body=profile.extra_body,
        )


llm_registry = LLMRegistry(llm_settings)


__all__ = [
    "LLMRegistry",
    "LLMRouter",
    "ProviderProfile",
    "ResolvedLLMRoute",
    "llm_registry",
]
