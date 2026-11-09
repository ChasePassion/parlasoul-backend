"""Shared request/log/observability context helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import Context, ContextVar, Token, copy_context
from dataclasses import dataclass
from typing import Any

_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
_REQUEST_METHOD: ContextVar[str | None] = ContextVar("request_method", default=None)
_REQUEST_PATH: ContextVar[str | None] = ContextVar("request_path", default=None)
_REQUEST_USER_ID: ContextVar[str | None] = ContextVar("request_user_id", default=None)
_LLM_CALL_ID: ContextVar[str | None] = ContextVar("llm_call_id", default=None)
_OPERATION: ContextVar[str | None] = ContextVar("operation", default=None)
_ROUTE_ROLE: ContextVar[str | None] = ContextVar("route_role", default=None)
_CHAT_ID: ContextVar[str | None] = ContextVar("chat_id", default=None)
_TURN_ID: ContextVar[str | None] = ContextVar("turn_id", default=None)
_CANDIDATE_ID: ContextVar[str | None] = ContextVar("candidate_id", default=None)
_CHARACTER_ID: ContextVar[str | None] = ContextVar("character_id", default=None)


@dataclass(frozen=True)
class ObservabilityContextSnapshot:
    """Snapshot of the current contextvars state for async tasks or threads."""

    context: Context

    def run(self, func, /, *args: Any, **kwargs: Any):
        return self.context.run(func, *args, **kwargs)


_FIELD_MAP: dict[str, ContextVar[str | None]] = {
    "request_id": _REQUEST_ID,
    "request_method": _REQUEST_METHOD,
    "request_path": _REQUEST_PATH,
    "user_id": _REQUEST_USER_ID,
    "llm_call_id": _LLM_CALL_ID,
    "operation": _OPERATION,
    "route_role": _ROUTE_ROLE,
    "chat_id": _CHAT_ID,
    "turn_id": _TURN_ID,
    "candidate_id": _CANDIDATE_ID,
    "character_id": _CHARACTER_ID,
}


def capture_observability_context() -> ObservabilityContextSnapshot:
    """Capture the current contextvars state for later reuse."""

    return ObservabilityContextSnapshot(context=copy_context())


def set_request_context(*, request_id: str, method: str, path: str) -> list[tuple[ContextVar[str | None], Token]]:
    """Bind request-scoped context fields."""

    return [
        (_REQUEST_ID, _REQUEST_ID.set(request_id)),
        (_REQUEST_METHOD, _REQUEST_METHOD.set(method)),
        (_REQUEST_PATH, _REQUEST_PATH.set(path)),
    ]


def set_request_user_id(user_id: str | None) -> Token:
    """Bind the authenticated user id for the current request/task."""

    return _REQUEST_USER_ID.set(user_id)


def reset_request_user_id(token: Token) -> None:
    _REQUEST_USER_ID.reset(token)


def clear_request_user_id() -> None:
    _REQUEST_USER_ID.set(None)


def set_llm_call_id(llm_call_id: str | None) -> Token:
    """Bind the current LLM call id."""

    return _LLM_CALL_ID.set(llm_call_id)


def reset_llm_call_id(token: Token) -> None:
    _LLM_CALL_ID.reset(token)


def reset_tokens(tokens: list[tuple[ContextVar[str | None], Token]]) -> None:
    """Reset a group of tokens in reverse order."""

    for var, token in reversed(tokens):
        var.reset(token)


@contextmanager
def bind_log_context(**fields: str | None) -> Iterator[None]:
    """Temporarily bind structured log context fields."""

    tokens: list[tuple[ContextVar[str | None], Token]] = []
    try:
        for key, value in fields.items():
            var = _FIELD_MAP.get(key)
            if var is None:
                continue
            tokens.append((var, var.set(value)))
        yield
    finally:
        reset_tokens(tokens)


def get_request_id() -> str | None:
    """Return the current request id when running inside a request context."""

    return _REQUEST_ID.get()


def current_log_context() -> dict[str, str]:
    """Return the current log context with empty values omitted."""

    context: dict[str, str] = {}
    for key, var in _FIELD_MAP.items():
        value = var.get()
        if value:
            context[key] = value
    return context
