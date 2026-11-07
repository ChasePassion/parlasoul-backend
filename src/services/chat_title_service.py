"""Dedicated chat title generation service."""

from __future__ import annotations

import re

from src.core.config import settings as app_settings
from src.core.llm_registry import LLMRegistry, llm_registry
from src.memory_system.prompts import CHAT_TITLE_PROMPT
from src.services.llm_gateway_service import LLMGatewayService

_MULTISPACE_RE = re.compile(r"\s+")


class ChatTitleService:
    """Generate one-time chat titles via routed LLM gateway."""

    def __init__(
        self,
        *,
        gateway_service: LLMGatewayService | None = None,
        registry: LLMRegistry | None = None,
    ) -> None:
        self._gateway_service = gateway_service or LLMGatewayService()
        self._registry = registry or llm_registry

    @staticmethod
    def build_prompt() -> str:
        return CHAT_TITLE_PROMPT.strip()

    @staticmethod
    def _strip_wrapper(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`").strip()
        return stripped.strip("\"''`")

    @staticmethod
    def clean_title(raw_title: str, *, max_length: int = app_settings.CHAT_TITLE_MAX_LENGTH) -> str | None:
        cleaned = ChatTitleService._strip_wrapper(raw_title or "")
        cleaned = cleaned.replace("\r", " ").replace("\n", " ")
        cleaned = _MULTISPACE_RE.sub(" ", cleaned).strip()
        if not cleaned:
            return None
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip()
        return cleaned or None

    async def generate_title(
        self,
        first_user_message: str,
        *,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
    ) -> str | None:
        message = (first_user_message or "").strip()
        if not message:
            return None

        route = self._registry.resolve_title_route()
        if route is None or not route.api_key:
            raise RuntimeError(
                f"{self._registry.router.title_provider.upper()} API key is required for chat title generation"
            )

        response = await self._gateway_service.generate_text(
            route=route,
            system_prompt=self.build_prompt(),
            user_message=message,
            operation="chat.title.generate",
            route_role="title",
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
        )
        return self.clean_title(response)
