"""Common API schema contracts."""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlmodel import SQLModel

T = TypeVar("T")


class SuccessEnvelope(SQLModel, Generic[T]):
    code: str = "ok"
    message: str = "ok"
    status: int
    data: T | None = None
