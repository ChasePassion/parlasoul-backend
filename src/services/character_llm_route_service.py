"""Resolve effective character LLM routes at response/build/runtime boundaries."""

from __future__ import annotations

from src.core.dialogue_styles import LLM_PRESET_FLAGSHIP
from src.core.exceptions import APIError
from src.core.llm_registry import LLMRegistry, ResolvedLLMRoute, llm_registry
from src.db.models import Character

ResolvedCharacterLLMRoute = ResolvedLLMRoute

# The "free" preset uses the system default chat route.
_FLAGSHIP_PROVIDER = "deepseek"
_FLAGSHIP_MODEL = "deepseek-v4-pro"


class CharacterLLMRouteService:
    """Resolves response-time and runtime LLM routes for characters."""

    def __init__(
        self,
        registry: LLMRegistry | None = None,
    ):
        self._registry = registry or llm_registry

    def default_route(self) -> ResolvedCharacterLLMRoute:
        route = self._registry.resolve_chat_route()
        if route is None or not route.api_key:
            provider = self._registry.router.chat_provider
            raise APIError(
                status_code=503,
                code="llm_route_unavailable",
                message=(
                    "dependency unavailable: default llm provider credentials are missing; "
                    f"provider={provider}"
                ),
            )
        return route

    async def resolve_runtime_route(self, character: Character) -> ResolvedCharacterLLMRoute:
        if getattr(character, "llm_preset_id", None) == LLM_PRESET_FLAGSHIP:
            return self._resolve_flagship_route()

        return self.default_route()

    def _resolve_flagship_route(self) -> ResolvedCharacterLLMRoute:
        """Resolve the flagship (premium) LLM route."""
        route = self._registry.resolve_explicit_route(
            provider=_FLAGSHIP_PROVIDER,
            model=_FLAGSHIP_MODEL,
            source="character_flagship",
        )
        if route is None or not route.api_key:
            raise APIError(
                status_code=503,
                code="llm_route_unavailable",
                message=(
                    "dependency unavailable: flagship llm provider credentials are missing; "
                    f"provider={_FLAGSHIP_PROVIDER}"
                ),
            )
        return route

    def resolve_vision_route(self) -> ResolvedCharacterLLMRoute:
        """Route to a vision-capable model (dashscope/qwen3.5-flash)."""
        route = self._registry.resolve_bound_route("dashscope:vision", source="vision")
        if route is None or not route.api_key:
            raise APIError(
                status_code=503,
                code="llm_route_unavailable",
                message="dependency unavailable: vision llm provider credentials are missing",
            )
        return route
