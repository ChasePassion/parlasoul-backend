"""Judge route resolver: fail-closed, no fallback."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.core.llm_registry import ResolvedLLMRoute
from src.evaluation.errors import JudgeRouteUnavailableError
from src.evaluation.judges.routes import JudgeRouteResolver


@dataclass(frozen=True)
class _Profile:
    enabled: bool = True
    api_key: str | None = "key-123"
    supports_structured_output: bool = True


class _StubRegistry:
    def __init__(
        self,
        *,
        profile: _Profile | None = _Profile(),
        selectable: list[str] | None = None,
        explicit_route: ResolvedLLMRoute | None = None,
    ) -> None:
        self._profile = profile
        self._selectable = selectable if selectable is not None else ["model-a"]
        self._explicit = explicit_route

    def get_profile(self, provider: str) -> _Profile | None:
        return self._profile

    def selectable_models(self, provider: str) -> list[str]:
        return self._selectable

    def resolve_explicit_route(self, *, provider: str, model: str, source: str):
        return self._explicit


def _route() -> ResolvedLLMRoute:
    return ResolvedLLMRoute(
        provider="deepseek",
        model="model-a",
        sdk_kind="openai_sdk",
        api_key="key-123",
        api_base=None,
        source="evaluation",
        model_type_label="deepseek:model-a",
    )


def test_resolves_locked_route() -> None:
    resolver = JudgeRouteResolver(_StubRegistry(explicit_route=_route()))

    resolved = resolver.resolve(provider="deepseek", model="model-a")

    assert resolved.provider == "deepseek"
    assert resolved.model == "model-a"
    assert resolved.api_key == "key-123"
    assert resolved.supports_structured_output is True


@pytest.mark.parametrize(
    ("provider", "model"),
    [(None, "model-a"), ("", "model-a"), ("deepseek", None), ("deepseek", "  ")],
)
def test_missing_identity_fails(provider: str | None, model: str | None) -> None:
    resolver = JudgeRouteResolver(_StubRegistry(explicit_route=_route()))
    with pytest.raises(JudgeRouteUnavailableError):
        resolver.resolve(provider=provider, model=model)


def test_unknown_provider_fails() -> None:
    resolver = JudgeRouteResolver(_StubRegistry(profile=None))
    with pytest.raises(JudgeRouteUnavailableError, match="unknown"):
        resolver.resolve(provider="ghost", model="model-a")


def test_disabled_provider_fails() -> None:
    resolver = JudgeRouteResolver(_StubRegistry(profile=_Profile(enabled=False)))
    with pytest.raises(JudgeRouteUnavailableError, match="disabled"):
        resolver.resolve(provider="deepseek", model="model-a")


def test_missing_api_key_fails() -> None:
    resolver = JudgeRouteResolver(_StubRegistry(profile=_Profile(api_key=None)))
    with pytest.raises(JudgeRouteUnavailableError, match="API key"):
        resolver.resolve(provider="deepseek", model="model-a")


def test_model_outside_selectable_fails() -> None:
    resolver = JudgeRouteResolver(
        _StubRegistry(selectable=["other-model"], explicit_route=_route())
    )
    with pytest.raises(JudgeRouteUnavailableError, match="selectable"):
        resolver.resolve(provider="deepseek", model="model-a")


def test_registry_refusal_fails() -> None:
    resolver = JudgeRouteResolver(_StubRegistry(explicit_route=None))
    with pytest.raises(JudgeRouteUnavailableError, match="resolution failed"):
        resolver.resolve(provider="deepseek", model="model-a")
