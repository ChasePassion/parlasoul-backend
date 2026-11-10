from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api.exceptions import register_exception_handlers
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.core.logging_context import reset_tokens, set_request_context
from src.memory_system.exceptions import LLMAuthenticationError
from src.services.chat_service import _sanitize_candidate_extra


class _ValidationPayload(BaseModel):
    count: int


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.middleware("http")
    async def _bind_request_context(request, call_next):
        tokens = set_request_context(
            request_id="req-test",
            method=request.method,
            path=request.url.path,
        )
        try:
            return await call_next(request)
        finally:
            reset_tokens(tokens)

    @app.get("/boom")
    async def boom() -> None:
        raise APIError(
            502,
            ErrorCode.LLM_SERVICE_ERROR,
            "upstream error: llm request failed after retries",
        )

    @app.post("/validate")
    async def validate(payload: _ValidationPayload) -> dict[str, int]:
        return {"count": payload.count}

    return app


def test_api_error_response_carries_request_id() -> None:
    with TestClient(_build_app()) as client:
        response = client.get("/boom")

    body = response.json()
    assert response.status_code == 502
    assert body["code"] == ErrorCode.LLM_SERVICE_ERROR
    assert body["request_id"] == "req-test"


def test_validation_response_uses_structured_rules_instead_of_pydantic_text() -> None:
    with TestClient(_build_app()) as client:
        response = client.post("/validate", json={"count": "not-a-number"})

    body = response.json()
    assert response.status_code == 422
    assert body["code"] == ErrorCode.VALIDATION_FAILED
    assert body["errors"][0]["field"] == "count"
    assert body["errors"][0]["rule"] == "invalid_type"
    assert "Input should be a valid integer" not in json.dumps(body, ensure_ascii=False)




def test_candidate_extra_sanitizer_drops_legacy_error_message() -> None:
    sanitized = _sanitize_candidate_extra(
        {"error_code": "chat_stream_failed", "error_message": "raw exception text"},
        include_learning_data=False,
    )

    assert sanitized is not None
    assert "error_message" not in sanitized
    assert sanitized["error_code"] == "chat_stream_failed"


@pytest.mark.parametrize("detail", [None, "upstream body text"])
def test_llm_auth_error_never_embeds_detail(detail: str | None) -> None:
    error = LLMAuthenticationError(
        model="m",
        provider="p",
        original_error=RuntimeError("boom"),
        detail=detail,
    )

    assert "upstream body text" not in error.public_message()
