"""Evaluation error types with safe, fixed-template messages.

Error messages must never include raw case text, credentials, exception
details or provider responses — only opaque IDs, field names, counts and
enumerated reasons.
"""

from __future__ import annotations

from src.core.error_codes import ErrorCode


class EvaluationError(Exception):
    """Base class for evaluation failures that map onto existing error codes."""

    code: str = ErrorCode.INTERNAL_ERROR

    def __init__(self, safe_message: str, *, code: str | None = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        if code is not None:
            self.code = code


class EvalConfigError(EvaluationError):
    """Invalid evaluation configuration (CLI flags, env allowlist)."""

    code = ErrorCode.VALIDATION_FAILED


class DatasetLoadError(EvaluationError):
    """Dataset file failed loading or validation; run must not start."""

    code = ErrorCode.VALIDATION_FAILED


class JudgeRouteUnavailableError(EvaluationError):
    """Judge route could not be resolved/locked; fail-closed before run."""

    code = ErrorCode.LLM_SERVICE_ERROR
