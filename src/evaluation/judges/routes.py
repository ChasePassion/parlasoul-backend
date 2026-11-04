"""Judge route resolution: one locked route per run, fail-closed.

``LLMRegistry.resolve_explicit_route`` alone is too permissive for evaluation
(it only checks provider-enabled and non-empty model); the resolver here adds
the selectable-model and API-key gates. There is no fallback: if the route is
unavailable the run never starts.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.llm_registry import LLMRegistry, llm_registry
from src.evaluation.errors import JudgeRouteUnavailableError

JUDGE_ROUTE_SOURCE = "evaluation"


@dataclass(frozen=True)
class ResolvedJudgeRoute:
    provider: str
    model: str
    sdk_kind: str
    api_key: str
    api_base: str | None
    model_type_label: str
    supports_structured_output: bool


class JudgeRouteResolver:
    def __init__(self, registry: LLMRegistry | None = None) -> None:
        self._registry = registry or llm_registry

    def resolve(self, *, provider: str | None, model: str | None) -> ResolvedJudgeRoute:
        provider_name = (provider or "").strip()
        model_name = (model or "").strip()
        if not provider_name or not model_name:
            raise JudgeRouteUnavailableError("judge route requires explicit provider and model")

        profile = self._registry.get_profile(provider_name)
        if profile is None:
            raise JudgeRouteUnavailableError("judge provider is unknown")
        if not profile.enabled:
            raise JudgeRouteUnavailableError("judge provider is disabled")
        if not profile.api_key:
            raise JudgeRouteUnavailableError("judge provider has no API key configured")
        if model_name not in self._registry.selectable_models(provider_name):
            raise JudgeRouteUnavailableError("judge model is not selectable for provider")

        route = self._registry.resolve_explicit_route(
            provider=provider_name, model=model_name, source=JUDGE_ROUTE_SOURCE
        )
        if route is None or not route.api_key:
            raise JudgeRouteUnavailableError("judge route resolution failed")

        return ResolvedJudgeRoute(
            provider=route.provider,
            model=route.model,
            sdk_kind=route.sdk_kind,
            api_key=route.api_key,
            api_base=route.api_base,
            model_type_label=route.model_type_label,
            supports_structured_output=profile.supports_structured_output,
        )
