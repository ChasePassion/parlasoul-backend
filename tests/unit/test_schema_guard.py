from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db.schema_guard import _REQUIRED_COLUMNS, assert_required_columns


def _rows_by_table(*, overrides: dict[str, list[str]] | None = None) -> dict[str, list[tuple[str]]]:
    rows = {
        table_name: [(column_name,) for column_name in sorted(required_columns)]
        for table_name, required_columns in _REQUIRED_COLUMNS.items()
    }

    if overrides:
        for table_name, column_names in overrides.items():
            rows[table_name] = [(column_name,) for column_name in column_names]

    return rows


@pytest.mark.asyncio
async def test_assert_required_columns_passes_when_column_exists() -> None:
    mapping = _rows_by_table()

    async def _execute(_query, params):
        table_name = params["table_name"]
        return mapping[table_name]

    conn = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        execute=AsyncMock(side_effect=_execute),
    )

    await assert_required_columns(conn)  # type: ignore[arg-type]

    assert conn.execute.await_count == len(_REQUIRED_COLUMNS)


@pytest.mark.asyncio
async def test_assert_required_columns_raises_when_column_missing() -> None:
    mapping = _rows_by_table(
        overrides={
            "user_settings": [
                "auto_read_aloud_enabled",
                "preferred_expression_bias_enabled",
                "reply_card_enabled",
            ]
        }
    )

    async def _execute(_query, params):
        table_name = params["table_name"]
        return mapping[table_name]

    conn = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        execute=AsyncMock(side_effect=_execute),
    )

    with pytest.raises(RuntimeError) as exc:
        await assert_required_columns(conn)  # type: ignore[arg-type]

    assert "user_settings.memory_enabled" in str(exc.value)
    assert conn.execute.await_count == len(_REQUIRED_COLUMNS)


@pytest.mark.asyncio
async def test_assert_required_columns_skips_non_postgresql() -> None:
    conn = SimpleNamespace(
        dialect=SimpleNamespace(name="sqlite"),
        execute=AsyncMock(),
    )

    await assert_required_columns(conn)  # type: ignore[arg-type]

    conn.execute.assert_not_called()
