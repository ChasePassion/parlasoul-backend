from __future__ import annotations

from unittest.mock import Mock

from src.memory_system.memory import Memory


def _build_memory() -> Memory:
    memory = Memory.__new__(Memory)
    memory._store = Mock()
    memory._narrative_manager = Mock()
    return memory


def test_delete_success_cleans_up_group_after_store_delete() -> None:
    memory = _build_memory()
    memory._store.query.return_value = [{"group_id": 9}]
    memory._store.delete.return_value = 1

    ok = memory.delete(memory_id=7, user_id="u1", character_id="c1")

    assert ok is True
    memory._store.query.assert_called_once_with(
        filter_expr='user_id == "u1" and character_id == "c1" and id == 7',
        output_fields=["group_id"],
        limit=1,
    )
    memory._store.delete.assert_called_once_with(ids=[7])
    memory._narrative_manager.cleanup_group.assert_called_once_with(
        user_id="u1",
        character_id="c1",
        group_id=9,
        exclude_memory_id=7,
    )


def test_delete_failure_does_not_cleanup_group() -> None:
    memory = _build_memory()
    memory._store.query.return_value = [{"group_id": 9}]
    memory._store.delete.return_value = 0

    ok = memory.delete(memory_id=7, user_id="u1", character_id="c1")

    assert ok is False
    memory._narrative_manager.cleanup_group.assert_not_called()


def test_delete_without_scope_skips_group_work() -> None:
    memory = _build_memory()
    memory._store.delete.return_value = 1

    ok = memory.delete(memory_id=11)

    assert ok is True
    memory._store.query.assert_not_called()
    memory._narrative_manager.cleanup_group.assert_not_called()
