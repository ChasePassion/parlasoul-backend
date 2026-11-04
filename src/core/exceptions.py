"""Core exception types shared across API, services, and dependencies."""

from __future__ import annotations


class APIError(Exception):
    """Domain API error with stable error code and HTTP status."""

    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


__all__ = ["APIError"]

