"""Image generation client: upstream refusals keep the original reason."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.core.exceptions import APIError
from src.repositories import image_generation_repository as image_module
from src.repositories.image_generation_repository import ImageGenerationRepository

_GENERATION_URL = "https://dashscope.test/generation"
_IMAGE_URL = "https://oss.test/output.png"


def _repository() -> ImageGenerationRepository:
    repo = ImageGenerationRepository.__new__(ImageGenerationRepository)
    repo._api_key = "test-key"
    repo._url = _GENERATION_URL
    repo._model = "qwen-image-3.0"
    repo._size = "2048*2048"
    repo._timeout = 30.0
    return repo


def _install_client(
    monkeypatch: pytest.MonkeyPatch, *, responses: list[httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs.pop("transport", None)
            super().__init__(transport=transport, **kwargs)

    monkeypatch.setattr(image_module.httpx, "AsyncClient", _Client)
    return requests


def _moderation_body() -> dict[str, Any]:
    return {
        "request_id": "fabb9dcd-a3db-9c2d-82f3-70bcd8998fd0",
        "code": "DataInspectionFailed",
        "message": "Green net check rejected image (input)",
    }


async def test_upstream_refusal_keeps_the_original_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(
        monkeypatch,
        responses=[httpx.Response(400, json=_moderation_body())],
    )
    repo = _repository()

    with pytest.raises(APIError) as excinfo:
        await repo.generate(prompt="a portrait")

    error = excinfo.value
    assert error.upstream_code == "DataInspectionFailed"
    assert error.upstream_message == "Green net check rejected image (input)"
    assert error.upstream_reason() == (
        "DataInspectionFailed / Green net check rejected image (input)"
    )


async def test_api_message_keeps_the_upstream_body_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(
        monkeypatch,
        responses=[httpx.Response(400, json=_moderation_body())],
    )
    repo = _repository()

    with pytest.raises(APIError) as excinfo:
        await repo.generate(prompt="a portrait")

    message = excinfo.value.message
    assert message == "upstream error: image generation failed; status=400"
    assert "Green net check rejected" not in message
    assert "DataInspectionFailed" not in message


async def test_non_json_upstream_body_falls_back_to_the_fixed_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(
        monkeypatch,
        responses=[httpx.Response(500, text="<html>bad gateway</html>")],
    )
    repo = _repository()

    with pytest.raises(APIError) as excinfo:
        await repo.generate(prompt="a portrait")

    error = excinfo.value
    assert error.upstream_code is None
    assert error.upstream_reason() is None
    assert "bad gateway" not in error.message


async def test_successful_generation_returns_the_image_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_client(
        monkeypatch,
        responses=[
            httpx.Response(
                200,
                json={
                    "output": {
                        "choices": [
                            {"message": {"content": [{"image": _IMAGE_URL}]}}
                        ]
                    }
                },
            ),
            httpx.Response(200, content=b"png-bytes"),
        ],
    )
    repo = _repository()

    raw = await repo.generate(prompt="a portrait")

    assert raw == b"png-bytes"
    assert [str(request.url) for request in requests] == [
        _GENERATION_URL,
        _IMAGE_URL,
    ]
