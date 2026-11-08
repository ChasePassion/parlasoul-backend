"""DeepSeek search / fetch client: request shape, response mapping, failures."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.core.exceptions import APIError
from src.repositories.deepseek_search_repository import DeepSeekSearchRepository

_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
_FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"


class _Recorder:
    """Mock transport handler that records the request it received."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.request: httpx.Request | None = None
        self.payload: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        self.payload = json.loads(request.content)
        return self.response


def _repository(recorder: _Recorder) -> DeepSeekSearchRepository:
    repo = DeepSeekSearchRepository()
    repo._api_key = "test-key"
    repo._base = "https://api.deepseek.com"
    repo._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return repo


async def test_search_sends_expected_request_and_maps_results():
    recorder = _Recorder(
        httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "标题",
                        "url": "https://example.com/a",
                        "date": "2026-01-02T03:04:05Z",
                        "snippet": "片段",
                        "summary": "概要",
                    }
                ]
            },
        )
    )
    repo = _repository(recorder)

    results = await repo.search(query="角色原型", num_results=3)

    assert recorder.request is not None
    assert str(recorder.request.url) == _SEARCH_URL
    assert recorder.request.headers["authorization"] == "Bearer test-key"
    assert recorder.payload == {
        "query": "角色原型",
        "count": 3,
        "need_content": False,
    }
    assert results == [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "published_date": "2026-01-02T03:04:05Z",
            "highlights": ["片段", "概要"],
        }
    ]


async def test_search_skips_empty_highlight_fields():
    recorder = _Recorder(
        httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "标题",
                        "url": "https://example.com/a",
                        "snippet": "",
                        "summary": "概要",
                    }
                ]
            },
        )
    )
    repo = _repository(recorder)

    results = await repo.search(query="q")

    assert results[0]["highlights"] == ["概要"]


async def test_search_clamps_requested_count_to_supported_range():
    recorder = _Recorder(httpx.Response(200, json={"results": []}))
    repo = _repository(recorder)

    await repo.search(query="q", num_results=50)
    assert recorder.payload["count"] == 8

    await repo.search(query="q", num_results=0)
    assert recorder.payload["count"] == 1


async def test_search_reports_upstream_failure_as_external_error():
    recorder = _Recorder(
        httpx.Response(500, json={"error": {"message": "boom", "code": "server_error"}})
    )
    repo = _repository(recorder)

    with pytest.raises(APIError) as excinfo:
        await repo.search(query="q")

    assert excinfo.value.status_code == 502
    assert excinfo.value.code == "external_error"


async def test_fetch_page_returns_markdown_content():
    recorder = _Recorder(
        httpx.Response(200, json={"title": "页面标题", "content": "# 正文"})
    )
    repo = _repository(recorder)

    page = await repo.fetch_page(url="https://example.com/a")

    assert recorder.request is not None
    assert str(recorder.request.url) == _FETCH_URL
    assert recorder.request.headers["authorization"] == "Bearer test-key"
    assert recorder.payload == {"url": "https://example.com/a"}
    assert page == {
        "url": "https://example.com/a",
        "title": "页面标题",
        "text": "# 正文",
    }


async def test_fetch_page_truncates_long_content():
    recorder = _Recorder(
        httpx.Response(200, json={"title": "页面标题", "content": "x" * 9000})
    )
    repo = _repository(recorder)

    page = await repo.fetch_page(url="https://example.com/a")

    assert len(page["text"]) == 8000


async def test_fetch_page_rejects_non_http_url():
    repo = _repository(_Recorder(httpx.Response(200, json={})))

    with pytest.raises(APIError) as excinfo:
        await repo.fetch_page(url="file:///etc/passwd")

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "validation_failed"


async def test_missing_api_key_is_reported_as_dependency_error():
    repo = DeepSeekSearchRepository()
    repo._api_key = None

    with pytest.raises(APIError) as excinfo:
        await repo.search(query="q")

    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "external_error"
