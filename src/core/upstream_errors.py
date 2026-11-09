"""Extraction of the original reason from an upstream error body.

Upstream services describe refusals in their own JSON shapes: DashScope
answers ``{"code": ..., "message": ...}``, DeepSeek nests the same fields
under ``{"error": {...}}``. The agent needs the original reason to tell the
user what actually failed, while the API error envelope must stay a fixed
summary. Both callers share this parser so the extracted detail is
consistent.
"""

from __future__ import annotations

import json
from typing import Any

_MAX_REASON_CHARS = 300


def _first_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_upstream_error(body: str) -> tuple[str | None, str | None]:
    """Extract ``(code, message)`` from an upstream JSON error body.

    Returns ``(None, None)`` when the body is not JSON or carries no
    recognisable reason, so callers can fall back to their fixed summary.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None

    nested = payload.get("error")
    candidates = [payload]
    if isinstance(nested, dict):
        candidates.insert(0, nested)

    code: str | None = None
    message: str | None = None
    for candidate in candidates:
        if code is None:
            code = _first_text(candidate, "code", "type")
        if message is None:
            message = _first_text(candidate, "message", "msg", "detail")

    if message and len(message) > _MAX_REASON_CHARS:
        message = message[:_MAX_REASON_CHARS]
    return code, message


__all__ = ["parse_upstream_error"]
