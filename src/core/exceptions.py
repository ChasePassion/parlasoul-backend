"""Core exception types shared across API, services, and dependencies."""

from __future__ import annotations


def _clean(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


class APIError(Exception):
    """Domain API error with stable error code and HTTP status.

    ``upstream_code`` and ``upstream_message`` hold the reason an upstream
    service reported when it refused a request. Only ``status_code``,
    ``code`` and ``message`` are rendered into the API response envelope;
    the upstream detail exists so the service layer can hand the original
    reason to the model, which is what lets it explain the failure to the
    user instead of guessing.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        upstream_code: str | None = None,
        upstream_message: str | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.upstream_code = _clean(upstream_code)
        self.upstream_message = _clean(upstream_message)
        super().__init__(message)

    def upstream_reason(self) -> str | None:
        """Return the upstream refusal reason, or ``None`` when there is none."""
        parts = [
            part for part in (self.upstream_code, self.upstream_message) if part
        ]
        return " / ".join(parts) or None


__all__ = ["APIError"]
