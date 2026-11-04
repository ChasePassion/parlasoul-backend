"""Centralized raw LLM environment settings."""

from __future__ import annotations

import json
import logging
from functools import cached_property
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.config import _resolve_env_file

logger = logging.getLogger(__name__)


class LLMSettings(BaseSettings):
    """Raw env-backed LLM provider settings.

    This module only knows provider-local variables:
    - model id
    - api key
    - base url

    Route binding decisions live in `llm_registry.py`.
    """

    OPENAI_API_KEY: str | None = None
    OPENAI_API_BASE: str | None = "https://api.openai.com/v1"
    OPENAI_MODEL_ID: str | None = "gpt-5.1-mini"

    LLM_DEEPSEEK_API_KEY: str | None = None
    LLM_DEEPSEEK_API_BASE: str | None = "https://api.deepseek.com"
    LLM_DEEPSEEK_MODEL_ID: str | None = "deepseek-v4-flash"
    # Optional provider-specific transport additions.  OpenCode Go, for
    # example, requires an ``x-opencode-session`` header on every request and
    # accepts ``reasoning_effort="none"`` to skip thinking tokens.
    LLM_DEEPSEEK_EXTRA_HEADERS: str | None = None
    LLM_DEEPSEEK_REASONING_EFFORT: str | None = None

    LLM_OPENROUTER_API_KEY: str | None = None
    LLM_OPENROUTER_API_BASE: str | None = "https://openrouter.ai/api/v1"
    LLM_OPENROUTER_MODEL_ID: str | None = "openai/gpt-4.1-mini"

    LLM_DASHSCOPE_API_KEY: str | None = None
    LLM_DASHSCOPE_API_BASE: str | None = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_DASHSCOPE_MODEL_ID: str | None = "qwen3.7-flash"
    LLM_DASHSCOPE_CHAT_MODEL_ID: str | None = None
    LLM_DASHSCOPE_TITLE_MODEL_ID: str | None = None
    LLM_DASHSCOPE_MEMORY_MODEL_ID: str | None = None
    LLM_DASHSCOPE_LEARNING_MODEL_ID: str | None = None
    LLM_DASHSCOPE_VISION_MODEL_ID: str | None = "qwen3.7-flash"

    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_BASE_URL: str | None = "https://api.minimaxi.com/anthropic"
    ANTHROPIC_MODEL_ID: str | None = "MiniMax-M2.5-highspeed"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    @staticmethod
    def _normalize_optional(value: str | None) -> str | None:
        candidate = (value or "").strip()
        return candidate or None

    @staticmethod
    def _normalize_openai_compatible_base(value: str | None) -> str | None:
        candidate = (value or "").strip().rstrip("/")
        if not candidate:
            return None
        suffix = "/chat/completions"
        if candidate.endswith(suffix):
            return candidate[: -len(suffix)]
        return candidate

    @cached_property
    def openai_api_key(self) -> str | None:
        return self._normalize_optional(self.OPENAI_API_KEY)

    @cached_property
    def openai_api_base(self) -> str | None:
        return self._normalize_optional(self.OPENAI_API_BASE)

    @cached_property
    def openai_model_id(self) -> str | None:
        return self._normalize_optional(self.OPENAI_MODEL_ID)

    @cached_property
    def deepseek_api_key(self) -> str | None:
        return self._normalize_optional(self.LLM_DEEPSEEK_API_KEY)

    @cached_property
    def deepseek_api_base(self) -> str | None:
        return self._normalize_optional(self.LLM_DEEPSEEK_API_BASE)

    @cached_property
    def deepseek_model_id(self) -> str | None:
        return self._normalize_optional(self.LLM_DEEPSEEK_MODEL_ID)

    @cached_property
    def deepseek_extra_headers(self) -> dict[str, str] | None:
        raw = (self.LLM_DEEPSEEK_EXTRA_HEADERS or "").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring LLM_DEEPSEEK_EXTRA_HEADERS: invalid JSON object")
            return None
        if not isinstance(payload, dict):
            logger.warning("Ignoring LLM_DEEPSEEK_EXTRA_HEADERS: expected a JSON object")
            return None
        headers = {
            str(key).strip(): value
            for key, value in payload.items()
            if str(key).strip() and isinstance(value, str)
        }
        return headers or None

    @cached_property
    def deepseek_extra_body(self) -> dict[str, Any] | None:
        effort = self._normalize_optional(self.LLM_DEEPSEEK_REASONING_EFFORT)
        if not effort:
            return None
        return {"reasoning_effort": effort}

    @cached_property
    def openrouter_api_key(self) -> str | None:
        return self._normalize_optional(self.LLM_OPENROUTER_API_KEY)

    @cached_property
    def openrouter_api_base(self) -> str | None:
        return self._normalize_optional(self.LLM_OPENROUTER_API_BASE)

    @cached_property
    def openrouter_model_id(self) -> str | None:
        return self._normalize_optional(self.LLM_OPENROUTER_MODEL_ID)

    @cached_property
    def dashscope_api_key(self) -> str | None:
        return self._normalize_optional(self.LLM_DASHSCOPE_API_KEY)

    @cached_property
    def dashscope_api_base(self) -> str | None:
        return self._normalize_openai_compatible_base(self.LLM_DASHSCOPE_API_BASE)

    @cached_property
    def dashscope_model_id(self) -> str | None:
        return self._normalize_optional(self.LLM_DASHSCOPE_MODEL_ID)

    @cached_property
    def dashscope_chat_model_id(self) -> str | None:
        return self._normalize_optional(self.LLM_DASHSCOPE_CHAT_MODEL_ID)

    @cached_property
    def dashscope_title_model_id(self) -> str | None:
        return self._normalize_optional(self.LLM_DASHSCOPE_TITLE_MODEL_ID)

    @cached_property
    def dashscope_memory_model_id(self) -> str | None:
        return self._normalize_optional(self.LLM_DASHSCOPE_MEMORY_MODEL_ID)

    @cached_property
    def dashscope_learning_model_id(self) -> str | None:
        return self._normalize_optional(self.LLM_DASHSCOPE_LEARNING_MODEL_ID)

    @cached_property
    def dashscope_vision_model_id(self) -> str | None:
        return self._normalize_optional(self.LLM_DASHSCOPE_VISION_MODEL_ID)

    @cached_property
    def anthropic_api_key(self) -> str | None:
        return self._normalize_optional(self.ANTHROPIC_API_KEY)

    @cached_property
    def anthropic_api_base(self) -> str | None:
        return self._normalize_optional(self.ANTHROPIC_BASE_URL)

    @cached_property
    def anthropic_model_id(self) -> str | None:
        return self._normalize_optional(self.ANTHROPIC_MODEL_ID)


llm_settings = LLMSettings(_env_file=_resolve_env_file())


__all__ = ["LLMSettings", "llm_settings"]
