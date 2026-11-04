"""Unified API response helpers."""

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from src.core.exceptions import APIError as APIError


def success_response(data: Any = None, status_code: int = 200, message: str = "ok") -> JSONResponse:
    """Build standard success response envelope."""
    payload = {
        "code": "ok",
        "message": message,
        "status": status_code,
        "data": data,
    }
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


def no_content_response() -> Response:
    """Build an HTTP 204 response with empty body."""
    return Response(status_code=204)


def error_body(status_code: int, code: str, message: str) -> dict[str, Any]:
    """Build standard error response body."""
    return {
        "code": code,
        "message": message,
        "status": status_code,
    }


SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
