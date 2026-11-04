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
    """Build normalized message from HTTPException detail payload."""
    if isinstance(detail, str) and detail.strip():
        return detail

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


def register_exception_handlers(app) -> None:
    """Register all exception handlers with the FastAPI app."""

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        first_error = exc.errors()[0] if exc.errors() else {}
        field_path = ".".join(str(part) for part in first_error.get("loc", [])) or "unknown"
        reason = first_error.get("msg", "invalid value")
        message = (
            "validation failed: request parameters are invalid; "
            f"field={field_path}; reason={reason}"
        )
        return JSONResponse(
            status_code=422,
            content=error_body(422, ErrorCode.VALIDATION_FAILED, message),
        )

    @app.exception_handler(MilvusConnectionError)
    async def milvus_connection_handler(request: Request, exc: MilvusConnectionError):
        logger.error("Milvus connection error: %s", exc)
        message = f"dependency unavailable: failed to connect to milvus; uri={exc.uri}"
        return JSONResponse(
            status_code=503,
            content=error_body(503, ErrorCode.DB_CONNECTION_ERROR, message),
        )

    @app.exception_handler(LLMCallError)
    async def llm_call_handler(request: Request, exc: LLMCallError):
        logger.error("LLM call error: %s", exc)
        message = (
            "upstream error: llm request failed after retries; "
            f"model={exc.model}; attempts={exc.attempts}"
        )
        return JSONResponse(
            status_code=502,
            content=error_body(502, ErrorCode.LLM_SERVICE_ERROR, message),
        )

    @app.exception_handler(LLMAuthenticationError)
    async def llm_auth_handler(request: Request, exc: LLMAuthenticationError):
        logger.error("LLM authentication error: %s", exc)
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
