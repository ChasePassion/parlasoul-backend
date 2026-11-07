"""Recursive allowlist sanitizer for every persisted text-shaped value.

Contract (phase18):
- ``none``: only numbers, booleans, None, enumerated statuses, opaque IDs and
  SHA-256 hashes survive; anything else is a violation and the artifact write
  aborts (fail-closed).
- ``redacted``: pre-sanitized sidecar text is additionally allowed (bounded).
- ``full``: synthetic/approved non-production text is allowed (bounded).
Raw model output must never reach persistence under ``none`` — rationale is
dropped to a hash upstream, errors use fixed templates.
"""

from __future__ import annotations

import re
from typing import Any

from src.evaluation.constants import (
    CONSOLIDATION_ACTIONS,
    CONSOLIDATION_OUTPUT_STATES,
    DIMENSIONS,
    OOC_DIMENSION,
    RUNNERS,
    SPLITS,
    TASK_TYPES,
)

_MAX_DEPTH = 12
_MAX_TEXT_CHARS_REDACTED = 12_000
_MAX_TEXT_CHARS_FULL = 12_000
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# Opaque identifiers and controlled slugs (case ids, scenario event names,
# safe-template separators like "&" and "="). No whitespace: prose text
# never matches.
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9._:\-=&]{1,200}$")
# ISO-8601 timestamps (selection windows etc.)
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$"
)


class SanitizerViolationError(Exception):
    """Raised when a value cannot be persisted under the content mode."""


_ENUM_STRINGS: frozenset[str] = frozenset(
    {
        # run / result / response / metric statuses
        "running",
        "completed",
        "partial",
        "failed",
        "cancelled",
        "no_data",
        "skipped",
        "valid",
        "runner_error",
        "judge_timeout",
        "judge_error",
        "judge_parse_failed",
        "judge_inconsistency",
        "available",
        "hook_required",
        "unavailable",
        "ok",
        "error",
        # content / run kinds / modes
        "none",
        "redacted",
        "full",
        "case",
        "aggregate",
        "locked",
        "none_route",
        # task identity
        *TASK_TYPES,
        *SPLITS,
        *RUNNERS,
        *DIMENSIONS,
        *CONSOLIDATION_ACTIONS,
        *CONSOLIDATION_OUTPUT_STATES,
        OOC_DIMENSION,
        "tie",
        "a",
        "b",
        "invalid",
        # misc metric vocabulary
        "episodic",
        "semantic",
        "all",
        "top_k",
        "determinism",
        "best_effort",
        "metric_kind",
        "memory_utility_pair",
        "repetition_3gram",
        "injection_hit_rate",
        "pairwise",
        "utility_delta",
        "fixture",
        "passed",
        # aggregate selection vocabulary
        "system",
        "metrics",
        "window_start",
        "window_end",
        "connection",
        "continuation_rate",
        "proactive_reply_rate",
        "turns_per_session",
        "active_turns_per_day",
        "return_d1",
        "return_d7",
    }
)


def sanitize_value(value: Any, *, content_mode: str, _depth: int = 0, _path: str = "$") -> Any:
    """Return a persistable copy of ``value`` or raise SanitizerViolationError.

    Violation messages carry the offending field path and length only —
    never the raw text itself.
    """
    if _depth > _MAX_DEPTH:
        raise SanitizerViolationError(f"{_path}: nesting exceeds sanitizer depth limit")

    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise SanitizerViolationError(f"{_path}: non-finite float is not persistable")
        return value
    if isinstance(value, str):
        try:
            return _sanitize_string(value, content_mode=content_mode)
        except SanitizerViolationError:
            raise SanitizerViolationError(
                f"{_path}: raw text is not persistable under content_mode={content_mode} "
                f"(len={len(value)})"
            ) from None
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SanitizerViolationError(f"{_path}: dict keys must be strings")
            sanitized[_sanitize_string(key, content_mode=content_mode, as_key=True)] = (
                sanitize_value(item, content_mode=content_mode, _depth=_depth + 1, _path=f"{_path}.{key}")
            )
        return sanitized
    if isinstance(value, list | tuple):
        return [
            sanitize_value(item, content_mode=content_mode, _depth=_depth + 1, _path=f"{_path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise SanitizerViolationError(f"{_path}: unsupported value type {type(value).__name__}")


def _sanitize_string(value: str, *, content_mode: str, as_key: bool = False) -> str:
    if value in _ENUM_STRINGS:
        return value
    if _HEX64_RE.match(value):
        return value
    if _ISO_DATETIME_RE.match(value):
        return value
    if content_mode == "none":
        # Opaque identifiers (case ids, refs like "case:<id>") are allowed only
        # when they carry no whitespace and stay short; everything else fails.
        if not as_key and _OPAQUE_ID_RE.match(value) and " " not in value:
            return value
        if as_key and _OPAQUE_ID_RE.match(value):
            return value
        raise SanitizerViolationError("raw text is not persistable under content_mode=none")
    limit = _MAX_TEXT_CHARS_REDACTED if content_mode == "redacted" else _MAX_TEXT_CHARS_FULL
    if len(value) > limit:
        raise SanitizerViolationError("text exceeds sanitizer length limit")
    return value


def sanitize_rationale(rationale: str | None, *, content_mode: str) -> str | None:
    """Judge rationale: None under ``none`` (hashing happens upstream),
    bounded text otherwise."""
    if rationale is None:
        return None
    if content_mode == "none":
        return None
    sanitized = sanitize_value(rationale, content_mode=content_mode)
    return sanitized[:4000]
