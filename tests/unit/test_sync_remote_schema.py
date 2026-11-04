from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sync_remote_schema.py"
SPEC = importlib.util.spec_from_file_location("sync_remote_schema", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
sync_remote_schema = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_remote_schema)


def test_normalize_pg_url_strips_async_driver_suffix() -> None:
    url = "postgresql+asyncpg://user:pass@localhost:5432/dbname"

    assert sync_remote_schema._normalize_pg_url(url) == "postgresql://user:pass@localhost:5432/dbname"


def test_redact_password_keeps_connection_usable() -> None:
    redacted, password = sync_remote_schema._redact_password(
        "postgresql://user:secret@localhost:5432/dbname?sslmode=require"
    )

    assert password == "secret"
    assert redacted == "postgresql://user@localhost:5432/dbname?sslmode=require"


def test_normalize_asyncpg_url_rewrites_asyncpg_ssl_query() -> None:
    normalized = sync_remote_schema._normalize_asyncpg_url(
        "postgresql+asyncpg://user:secret@localhost:5432/dbname?sslmode=require&channel_binding=require"
    )

    assert normalized == "postgresql+asyncpg://user:secret@localhost:5432/dbname?ssl=require"


def test_assert_distinct_databases_rejects_same_target() -> None:
    with pytest.raises(ValueError, match="same host/port/database"):
        sync_remote_schema._assert_distinct_databases(
            "postgresql://user@localhost:5432/role_play_mem",
            "postgresql://other@localhost:5432/role_play_mem",
        )
