"""DeepSeek web search / page fetch for the character-forge agent."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from src.core.config import settings
from src.core.exceptions import APIError
from src.core.upstream_errors import parse_upstream_error

logger = logging.getLogger(__name__)

_MAX_HIGHLIGHT_CHARS = 1200
_MAX_PAGE_CHARS = 8000
_SEARCH_PATH = "/v1/tools/search"
_FETCH_PATH = "/v1/tools/fetch"


class DeepSeekSearchRepository:
    """Thin HTTP client for the two DeepSeek tool endpoints the agent needs."""

    def __init__(self) -> None:
        self._api_key = settings.DEEPSEEK_SEARCH_API_KEY
        self._base = settings.DEEPSEEK_SEARCH_API_BASE.rstrip("/")
        self._timeout = settings.DEEPSEEK_SEARCH_TIMEOUT_SECONDS
        self._proxy = settings.DEEPSEEK_SEARCH_PROXY

    def _require_key(self) -> str:
        if not self._api_key or not self._api_key.strip():
            raise APIError(
                status_code=503,
                code="external_error",
                message=(
                    "dependency unavailable: DEEPSEEK_SEARCH_API_KEY is not configured"
                ),
            )
        return self._api_key.strip()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            proxy=self._proxy,
            trust_env=False,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self._require_key()
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{self._base}{path}",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                raise APIError(
                    status_code=504,
                    code="external_timeout",
                    message="upstream error: deepseek search request timed out",
                ) from exc
            except httpx.HTTPError as exc:
                logger.exception("deepseek search request failed: %s", exc)
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message="upstream error: deepseek search request failed",
                ) from exc

            if response.status_code != 200:
                upstream_code, upstream_message = parse_upstream_error(response.text)
                logger.warning(
                    "deepseek search failed: path=%s status=%s upstream_code=%s body=%s",
                    path,
                    response.status_code,
                    upstream_code,
                    response.text[:300],
                )
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message=(
                        "upstream error: deepseek search failed; "
                        f"status={response.status_code}"
                    ),
                    upstream_code=upstream_code,
                    upstream_message=upstream_message,
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise APIError(
                    status_code=502,
                    code="external_error",
                    message="upstream error: deepseek search returned invalid json",
                ) from exc

        return data if isinstance(data, dict) else {}

    async def search(
        self, *, query: str, num_results: int = 5
    ) -> list[dict[str, Any]]:
        data = await self._post(
            _SEARCH_PATH,
            {
                "query": query,
                "count": max(1, min(8, num_results)),
                "need_content": False,
            },
        )

        results: list[dict[str, Any]] = []
        for item in data.get("results") or []:
            highlights: list[str] = []
            for chunk in (item.get("snippet"), item.get("summary")):
                text = str(chunk or "").strip()
                if text:
                    highlights.append(text[:_MAX_HIGHLIGHT_CHARS])
            results.append(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "published_date": item.get("date"),
                    "highlights": highlights,
                }
            )
        return results

    async def fetch_page(self, *, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: url must be an absolute http(s) URL",
            )
        data = await self._post(_FETCH_PATH, {"url": url})
        return {
            "url": url,
            "title": data.get("title") or None,
            "text": (data.get("content") or "")[:_MAX_PAGE_CHARS],
        }
