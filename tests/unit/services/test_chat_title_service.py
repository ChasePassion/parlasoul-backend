from __future__ import annotations

import pytest

from src.core.llm_registry import ResolvedLLMRoute
from src.services.chat_title_service import ChatTitleService


class _StubGateway:
    def __init__(self, response: str) -> None:
        self._response = response

    async def generate_text(
        self,
        *,
        route,
        system_prompt: str,
        user_message: str,
        **kwargs,
    ) -> str:
        assert route
        assert system_prompt
        assert user_message
        return self._response


class _StubRegistry:
    def __init__(self, route: ResolvedLLMRoute | None) -> None:
        self._route = route
        self.router = type("RouterStub", (), {"title_provider": "openrouter"})()

    def resolve_title_route(self) -> ResolvedLLMRoute | None:
        return self._route


def _build_route() -> ResolvedLLMRoute:
    return ResolvedLLMRoute(
        provider="openrouter",
        model="openai/gpt-4.1-mini",
        sdk_kind="pydantic_ai",
        api_key="openrouter-key",
        api_base="https://openrouter.ai/api/v1",
        source="system_title",
        model_type_label="openrouter:openai/gpt-4.1-mini",
    )




@pytest.mark.asyncio
async def test_generate_title_uses_llm_and_cleans_output() -> None:
    service = ChatTitleService(
        gateway_service=_StubGateway('  "旅行计划讨论"  '),
        registry=_StubRegistry(_build_route()),
    )

    title = await service.generate_title("Help me plan a trip to Tokyo")

    assert title == "旅行计划讨论"


@pytest.mark.asyncio
async def test_generate_title_raises_when_route_is_not_configured() -> None:
    service = ChatTitleService(
        gateway_service=_StubGateway("ignored"),
        registry=_StubRegistry(None),
    )

    with pytest.raises(RuntimeError):
        await service.generate_title("Help me plan a trip to Tokyo")
