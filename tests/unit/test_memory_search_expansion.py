from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from src.memory_system.memory import Memory


def _record(
    *,
    memory_id: int,
    user_id: str,
    character_id: str,
    memory_type: str,
    ts: int,
    chat_id: str,
    text: str,
    group_id: int,
) -> dict:
    return {
        "id": memory_id,
        "user_id": user_id,
        "character_id": character_id,
        "memory_type": memory_type,
        "ts": ts,
        "chat_id": chat_id,
        "text": text,
        "group_id": group_id,
    }


def test_search_expands_groups_with_single_batch_query_and_stable_order() -> None:
    memory = Memory.__new__(Memory)
    memory._config = SimpleNamespace(use_all_semantic=False, k_semantic=5, k_episodic=5)
    memory._embedding_client = Mock()
    memory._store = Mock()

    memory._embedding_client.encode.return_value = [[1.0, 0.0]]
    del memory._embedding_client.encode_with_usage
    memory._store.search.side_effect = [
        [
            [
                _record(
                    memory_id=900,
                    user_id="u1",
                    character_id="c1",
                    memory_type="semantic",
                    ts=10,
                    chat_id="chat-sem",
                    text="user likes tea",
                    group_id=-1,
                )
            ]
        ],
        [
            [
                _record(
                    memory_id=1,
                    user_id="u1",
                    character_id="c1",
                    memory_type="episodic",
                    ts=20,
                    chat_id="chat-1",
                    text="seed-1",
                    group_id=10,
                ),
                _record(
                    memory_id=2,
                    user_id="u1",
                    character_id="c1",
                    memory_type="episodic",
                    ts=19,
                    chat_id="chat-2",
                    text="seed-2",
                    group_id=20,
                ),
            ]
        ],
    ]
    memory._store.query.side_effect = [
        [{"id": 4}, {"id": 3}, {"id": 1}],
        [
            _record(
                memory_id=3,
                user_id="u1",
                character_id="c1",
                memory_type="episodic",
                ts=18,
                chat_id="chat-3",
                text="exp-3",
                group_id=10,
            ),
            _record(
                memory_id=4,
                user_id="u1",
                character_id="c1",
                memory_type="episodic",
                ts=17,
                chat_id="chat-4",
                text="exp-4",
                group_id=20,
            ),
            _record(
                memory_id=2,
                user_id="u1",
                character_id="c1",
                memory_type="episodic",
                ts=19,
                chat_id="chat-2",
                text="seed-2",
                group_id=20,
            ),
            _record(
                memory_id=1,
                user_id="u1",
                character_id="c1",
                memory_type="episodic",
                ts=20,
                chat_id="chat-1",
                text="seed-1",
                group_id=10,
            ),
        ],
    ]

    result = memory.search(query="q", user_id="u1", character_id="c1", chat_id="chat-ctx")

    assert [m.id for m in result["episodic"]] == [1, 2, 3, 4]
    assert [m.id for m in result["semantic"]] == [900]

    assert memory._store.query.call_count == 2
    assert memory._store.query.call_args_list[0].kwargs["filter_expr"] == (
        'user_id == "u1" and character_id == "c1" and group_id in [10, 20]'
    )
    assert memory._store.query.call_args_list[1].kwargs["filter_expr"] == (
        'user_id == "u1" and character_id == "c1" and id in [1, 2, 3, 4]'
    )
