from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from src.memory_system.processors.narrative_memory_manager import NarrativeMemoryManager


def test_assign_to_narrative_group_batches_memory_fetch_and_recomputes_once(
) -> None:
    store = Mock()
    store._collection_name = "memories"
    store.create_groups_collection.return_value = "groups"
    store.query.side_effect = [
        [
            {"id": 1, "group_id": -1, "vector": [1.0, 0.0, 0.0]},
            {"id": 2, "group_id": -1, "vector": [0.9, 0.1, 0.0]},
        ],
        [
            {"id": 1, "vector": [1.0, 0.0, 0.0]},
            {"id": 2, "vector": [0.9, 0.1, 0.0]},
        ],
    ]
    store.search_groups.side_effect = [
        [{"group_id": 42, "sim": 0.95, "size": 3}],
        [{"group_id": 42, "sim": 0.91, "size": 4}],
    ]
    store.update_memory_group_id.return_value = True
    store.update_group.return_value = True

    config = SimpleNamespace(embedding_dim=3, narrative_similarity_threshold=0.8)
    manager = NarrativeMemoryManager(store, config)

    result = manager.assign_to_narrative_group(
        memory_ids=[1, 2],
        user_id="u1",
        character_id="c1",
    )

    assert result == {1: 42, 2: 42}
    assert store.query.call_count == 2
    assert store.query.call_args_list[0].kwargs["filter_expr"] == (
        'user_id == "u1" and character_id == "c1" and id in [1, 2]'
    )
    assert store.query.call_args_list[1].kwargs["filter_expr"] == (
        'user_id == "u1" and character_id == "c1" and group_id == 42'
    )
    assert store.update_memory_group_id.call_count == 2
    store.update_group.assert_called_once()
    assert store.update_group.call_args.kwargs["user_id"] == "u1"
    assert store.update_group.call_args.kwargs["character_id"] == "c1"
    assert store.update_group.call_args.kwargs["group_id"] == 42
    assert store.update_group.call_args.kwargs["size"] == 2
