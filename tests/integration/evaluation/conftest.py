"""Shared helpers for evaluation PG integration tests.

The isolated per-test schemas run ``create_all`` against models whose PG
enum types are declared with ``create_type=False``. On a pristine database
(CI) those types do not exist, so the fixtures must create them first; on
databases where they already exist (dev, public schema) creation is skipped.
"""

from __future__ import annotations

import pytest
from sqlalchemy.sql import text as sql_text

PG_ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "visibility_t": ("PUBLIC", "PRIVATE"),
    "chat_visibility_t": ("PUBLIC", "PRIVATE"),
    "chat_type_t": ("ONE_ON_ONE", "ROOM"),
    "chat_state_t": ("ACTIVE", "ARCHIVED"),
    "author_type_t": ("USER", "CHARACTER", "SYSTEM"),
    "turn_state_t": ("OK", "FILTERED", "DELETED", "ERROR"),
}


@pytest.fixture()
def ensure_pg_enum_types():
    """Return an async helper that creates missing PG enum types."""

    async def _ensure(conn) -> None:
        for name, labels in PG_ENUM_TYPES.items():
            exists = await conn.execute(
                sql_text("SELECT 1 FROM pg_type WHERE typname = :name"), {"name": name}
            )
            if exists.first() is None:
                label_sql = ", ".join(f"'{label}'" for label in labels)
                await conn.execute(sql_text(f"CREATE TYPE {name} AS ENUM ({label_sql})"))

    return _ensure
