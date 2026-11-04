"""Independent runtime LLM gateway for character reply generation."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from src.services.character_llm_route_service import ResolvedCharacterLLMRoute
from src.services.llm_gateway_service import LLMGatewayService


class LLMGatewayRepository:
    """Provides per-route LLM clients without mutating the memory singleton."""

    def __init__(self, gateway_service: LLMGatewayService | None = None) -> None:
        self._gateway_service = gateway_service or LLMGatewayService()

    async def chat_stream_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        route: ResolvedCharacterLLMRoute,
        operation: str,
        route_role: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
    ) -> AsyncGenerator[str, None]:
        async for chunk in self._gateway_service.stream_text(
            route=route,
            messages=messages,
            operation=operation,
            route_role=route_role,
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
        ):
            yield chunk

    async def generate_text(
        self,
        *,
        route: ResolvedCharacterLLMRoute,
        system_prompt: str,
        user_message: str,
        operation: str,
        route_role: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
    ) -> str:
        return await self._gateway_service.generate_text(
            route=route,
            system_prompt=system_prompt,
            user_message=user_message,
            operation=operation,
            route_role=route_role,
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
        )
