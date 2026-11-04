from __future__ import annotations

from unittest.mock import Mock

from src.memory_system.memory import Memory


def _build_memory() -> Memory:
    memory = Memory.__new__(Memory)
    memory._store = Mock()
    return memory


def test_reset_deletes_memories_and_groups() -> None:
    memory = _build_memory()
    memory._store.delete.return_value = 4
    memory._store.delete_groups_for_scope.return_value = 2

    count = memory.reset(user_id="u1", character_id="c1")

    assert count == 4
    memory._store.delete.assert_called_once_with(
        filter_expr='user_id == "u1" and character_id == "c1"'
    )
    memory._store.delete_groups_for_scope.assert_called_once_with(
        user_id="u1",
        character_id="c1",
    )


def test_reset_keeps_success_when_group_cleanup_fails() -> None:
    memory = _build_memory()
    memory._store.delete.return_value = 1
    memory._store.delete_groups_for_scope.side_effect = RuntimeError("groups err")

    count = memory.reset(user_id="u2", character_id="c2")

    assert count == 1
    memory._store.delete.assert_called_once_with(
        filter_expr='user_id == "u2" and character_id == "c2"'
    )
    memory._store.delete_groups_for_scope.assert_called_once_with(
        user_id="u2",
        character_id="c2",
    )
