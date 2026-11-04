"""High-level character reply generation helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from src.db.models import Character
from src.repositories.llm_gateway_repository import LLMGatewayRepository
from src.services.character_llm_route_service import (
    CharacterLLMRouteService,
    ResolvedCharacterLLMRoute,
)


class CharacterLLMService:
    def __init__(
        self,
        route_service: CharacterLLMRouteService | None = None,
        gateway_repo: LLMGatewayRepository | None = None,
    ) -> None:
        self._route_service = route_service or CharacterLLMRouteService()
        self._gateway_repo = gateway_repo or LLMGatewayRepository()

    async def resolve_runtime_route(self, character: Character) -> ResolvedCharacterLLMRoute:
        return await self._route_service.resolve_runtime_route(character)

    def resolve_vision_route(self) -> ResolvedCharacterLLMRoute:
        return self._route_service.resolve_vision_route()

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
        async for chunk in self._gateway_repo.chat_stream_messages(
            messages=messages,
            route=route,
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
        return await self._gateway_repo.generate_text(
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
