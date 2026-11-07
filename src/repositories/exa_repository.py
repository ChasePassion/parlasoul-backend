"""Exa web search / content extraction for the character-forge agent."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from src.core.config import settings
from src.core.exceptions import APIError

logger = logging.getLogger(__name__)

_MAX_HIGHLIGHT_CHARS = 1200
_MAX_PAGE_CHARS = 8000


class ExaRepository:
    """Thin HTTP client for the two Exa endpoints the agent needs."""

    def __init__(self) -> None:
        self._api_key = settings.EXA_API_KEY
        self._base = settings.EXA_API_BASE.rstrip("/")
        self._timeout = settings.EXA_TIMEOUT_SECONDS
        self._proxy = settings.EXA_PROXY

    def _require_key(self) -> str:
        if not self._api_key or not self._api_key.strip():
            raise APIError(
                status_code=503,
                code="external_error",
                message="dependency unavailable: EXA_API_KEY is not configured",
            )
        return self._api_key.strip()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            proxy=self._proxy,
            trust_env=False,
        )

    async def search(self, *, query: str, num_results: int = 5) -> list[dict[str, Any]]:
        api_key = self._require_key()
        payload = {
            "query": query,
            "type": "auto",
            "numResults": max(1, min(8, num_results)),
            "contents": {"highlights": True},
        }
        async with self._client() as client:
            response = await client.post(
                f"{self._base}/search",
                json=payload,
                headers={"x-api-key": api_key},
            )
            response.raise_for_status()
            data = response.json()

        results: list[dict[str, Any]] = []
        for item in data.get("results") or []:
            highlights = [
                str(chunk)[:_MAX_HIGHLIGHT_CHARS]
                for chunk in (item.get("highlights") or [])
                if chunk
            ]
            results.append(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "published_date": item.get("publishedDate"),
                    "highlights": highlights,
                }
            )
        return results

    async def get_contents(self, *, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: url must be an absolute http(s) URL",
            )
        api_key = self._require_key()
        async with self._client() as client:
            response = await client.post(
                f"{self._base}/contents",
                json={"urls": [url], "text": {"maxCharacters": _MAX_PAGE_CHARS}},
                headers={"x-api-key": api_key},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("results") or []
        if not results:
            return {"url": url, "title": None, "text": ""}
        item = results[0]
        return {
            "url": item.get("url") or url,
            "title": item.get("title"),
            "text": (item.get("text") or "")[:_MAX_PAGE_CHARS],
        }
