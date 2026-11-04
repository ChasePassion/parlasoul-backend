"""OpenRouter model directory fetch with local cache."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from src.core.config import settings
from src.core.exceptions import APIError
from src.core.llm_settings import llm_settings


class OpenRouterModelRepository:
    """Fetches OpenRouter model directory with cache and stale fallback."""

    _cache_lock = asyncio.Lock()
    _cached_items: list[dict[str, Any]] = []
    _cache_expire_at: float = 0.0

    @staticmethod
    def _models_user_url() -> str:
        base = (llm_settings.openrouter_api_base or "https://openrouter.ai/api/v1").rstrip("/")
        if base.endswith("/api/v1"):
            return f"{base}/models/user"
        return f"{base}/api/v1/models/user"

    @staticmethod
    def _headers() -> dict[str, str]:
        api_key = llm_settings.openrouter_api_key
        if not api_key:
            raise APIError(
                status_code=503,
                code="llm_provider_unsupported",
                message="dependency unavailable: openrouter api key is missing",
            )
        return {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    def _cache_seconds() -> int:
        raw = getattr(settings, "LLM_OPENROUTER_MODELS_CACHE_SECONDS", 600)
        try:
            return max(60, int(raw))
        except Exception:
            return 600

    @classmethod
    def _normalize_items(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, list):
            raise APIError(
                status_code=503,
                code="llm_provider_invalid_response",
                message="dependency unavailable: openrouter model directory returned invalid payload",
            )

        items: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue

            architecture = item.get("architecture")
            if isinstance(architecture, dict):
                output_modalities = architecture.get("output_modalities")
                if isinstance(output_modalities, list) and output_modalities and "text" not in output_modalities:
                    continue

            items.append(item)
        return items

    async def list_models_for_user(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._cached_items and now < self._cache_expire_at:
            return list(self._cached_items)

        async with self._cache_lock:
            now = time.time()
            if self._cached_items and now < self._cache_expire_at:
                return list(self._cached_items)

            try:
                async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_SECONDS) as client:
                    response = await client.get(self._models_user_url(), headers=self._headers())
                response.raise_for_status()
                items = self._normalize_items(response.json())
                self._cached_items = items
                self._cache_expire_at = now + self._cache_seconds()
                return list(items)
            except APIError:
                raise
            except Exception as exc:
                if self._cached_items:
                    return list(self._cached_items)
                raise APIError(
                    status_code=503,
                    code="llm_provider_unavailable",
                    message=f"dependency unavailable: failed to fetch openrouter model directory; err={exc}",
                )
