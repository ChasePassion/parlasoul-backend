from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import src.repositories.memory_repository as memory_repository_module
from src.memory_system.utils import build_filter, escape_milvus_string
from src.repositories.memory_repository import MemoryRepository


def test_escape_milvus_string_escapes_special_chars() -> None:
    raw = 'user"\\name\nnext\tline\rend'
    escaped = escape_milvus_string(raw)
    assert escaped == 'user\\"\\\\name\\nnext\\tline\\rend'


def test_build_filter_escapes_user_and_character_ids() -> None:
    expr = build_filter('u"1\\x', 'c"2\\y', "id == 1")
    assert expr == 'user_id == "u\\"1\\\\x" and character_id == "c\\"2\\\\y" and id == 1'


@pytest.mark.asyncio
async def test_repository_exists_uses_safe_scoped_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_store = SimpleNamespace(query=Mock())
    fake_memory = SimpleNamespace(store=fake_store)
    repo = MemoryRepository(fake_memory)

    captured: dict[str, object] = {}

    async def fake_run_blocking(func, /, *args, **kwargs):
        captured["func"] = func
        captured["kwargs"] = kwargs
        return [{"id": 1}]

    monkeypatch.setattr(memory_repository_module, "run_blocking", fake_run_blocking)

    ok = await repo.exists(
        memory_id=5,
        user_id='u"1\\x',
        character_id='c"2\\y',
    )

    assert ok is True
    assert captured["func"] is fake_store.query
    assert captured["kwargs"] == {
        "filter_expr": 'user_id == "u\\"1\\\\x" and character_id == "c\\"2\\\\y" and id == 5',
        "output_fields": ["id"],
        "limit": 1,
    }
