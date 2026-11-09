"""Global exception handlers with unified error envelope."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.api.response import error_body
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.memory_system.exceptions import (
    LLMAuthenticationError,
    LLMCallError,
    MilvusConnectionError,
)

logger = logging.getLogger(__name__)


def _default_error_code(status_code: int) -> str:
    """Map HTTP status to default error code when source is unknown."""
    mapping = {
        400: ErrorCode.INVALID_PARAM,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.RESOURCE_NOT_FOUND,
        409: ErrorCode.RESOURCE_CONFLICT,
        413: ErrorCode.UPLOAD_FILE_TOO_LARGE,
        415: ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
        422: ErrorCode.VALIDATION_FAILED,
        429: ErrorCode.TOO_MANY_REQUESTS,
        500: ErrorCode.INTERNAL_ERROR,
        502: ErrorCode.EXTERNAL_ERROR,
        503: ErrorCode.DB_CONNECTION_ERROR,
        504: ErrorCode.EXTERNAL_TIMEOUT,
    }
    return mapping.get(status_code, ErrorCode.INTERNAL_ERROR)


def _normalize_http_message(status_code: int, detail: object) -> str:
    """Build a fixed, client-safe message for HTTPException responses.

    ``detail`` is intentionally ignored for string payloads: it may carry
    internal exception text from call sites that should use ``APIError``
    instead. The fixed fallback keeps the response contract stable.
    """
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            return message

    fallback = {
        400: "invalid request: request data is not valid",
        401: "authorization failed: credentials are missing or invalid",
        403: "authorization failed: operation is forbidden",
        404: "resource not found: requested resource does not exist",
        409: "resource conflict: operation conflicts with current state",
        422: "validation failed: request parameters are invalid",
    }
    return fallback.get(status_code, "internal error: unexpected server error")


_VALIDATION_RULE_BY_TYPE: dict[str, str] = {
    "missing": "required",
    "extra_forbidden": "not_allowed",
    "too_long": "length_out_of_range",
    "too_short": "length_out_of_range",
    "greater_than": "out_of_range",
    "greater_than_equal": "out_of_range",
    "less_than": "out_of_range",
    "less_than_equal": "out_of_range",
    "value_error": "invalid_value",
}


def _validation_rule(error_type: object) -> str:
    """Map a pydantic error type to a stable, language-neutral rule name."""
    if not isinstance(error_type, str) or not error_type:
        return "invalid"
    if error_type in _VALIDATION_RULE_BY_TYPE:
        return _VALIDATION_RULE_BY_TYPE[error_type]
    if error_type.endswith("_type") or error_type.endswith("_parsing"):
        return "invalid_type"
    return "invalid_value" if error_type.endswith("_enum") else "invalid"


def _validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    """Build structured field/rule entries without pydantic's English text."""
    details: list[dict[str, str]] = []
    for error in exc.errors():
        location = [
            str(part)
            for part in error.get("loc", [])
            if part not in {"body", "query", "path", "header", "cookie"}
        ]
        details.append(
            {
                "field": ".".join(location) or "request",
                "rule": _validation_rule(error.get("type")),
            }
        )
    return details


def register_exception_handlers(app) -> None:
    """Register all exception handlers with the FastAPI app."""

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        logger.warning("Request validation failed: %s", exc.errors())
        message = "validation failed: request parameters are invalid"
        return JSONResponse(
            status_code=422,
            content=error_body(
                422,
                ErrorCode.VALIDATION_FAILED,
                message,
                errors=_validation_errors(exc),
            ),
        )

    @app.exception_handler(MilvusConnectionError)
    async def milvus_connection_handler(request: Request, exc: MilvusConnectionError):
        logger.error("Milvus connection error: %s", exc)
        message = "dependency unavailable: failed to connect to milvus"
        return JSONResponse(
            status_code=503,
            content=error_body(503, ErrorCode.DB_CONNECTION_ERROR, message),
        )

    @app.exception_handler(LLMCallError)
    async def llm_call_handler(request: Request, exc: LLMCallError):
        logger.error("LLM call error: %s", exc)
        message = "upstream error: llm request failed after retries"
        return JSONResponse(
            status_code=502,
            content=error_body(502, ErrorCode.LLM_SERVICE_ERROR, message),
        )

    @app.exception_handler(LLMAuthenticationError)
    async def llm_auth_handler(request: Request, exc: LLMAuthenticationError):
        logger.error(
            "LLM authentication error: model=%s provider=%s detail=%s",
            exc.model,
            exc.provider,
            exc.detail,
        )
        return JSONResponse(
            status_code=502,
            content=error_body(502, ErrorCode.LLM_SERVICE_ERROR, exc.public_message()),
        )

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.status_code, exc.code, exc.message),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        status_code = exc.status_code
        code = _default_error_code(status_code)
        message = _normalize_http_message(status_code, exc.detail)
        return JSONResponse(
            status_code=status_code,
            content=error_body(status_code, code, message),
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content=error_body(
                500,
                ErrorCode.INTERNAL_ERROR,
                "internal error: unexpected server error",
            ),
        )
