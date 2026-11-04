from __future__ import annotations

from unittest.mock import Mock

from src.memory_system.clients.embedding import EmbeddingResult
from src.memory_system.memory import Memory


class _LegacyEmbeddingClientStub:
    def __init__(self) -> None:
        self.encode = Mock()


def _build_memory() -> Memory:
    memory = Memory.__new__(Memory)
    memory._store = Mock()
    memory._embedding_client = _LegacyEmbeddingClientStub()
    memory._narrative_manager = Mock()
    return memory


def test_update_in_place_success_and_group_cleanup() -> None:
    memory = _build_memory()
    memory._store.query.return_value = [
        {
            "id": 7,
            "user_id": "u1",
            "character_id": "c1",
            "memory_type": "episodic",
            "ts": 123,
            "chat_id": "chat-1",
            "text": "old text",
            "group_id": 23,
        }
    ]
    memory._embedding_client.encode.return_value = [[0.1, 0.2, 0.3]]
    memory._store.update_memory.return_value = True

    ok = memory.update(
        memory_id=7,
        data={"text": "new text"},
        user_id="u1",
        character_id="c1",
    )

    assert ok is True

    memory._store.query.assert_called_once()
    memory._embedding_client.encode.assert_called_once_with(["new text"])
    memory._store.update_memory.assert_called_once()
    patch = memory._store.update_memory.call_args.kwargs["patch"]
    assert patch["text"] == "new text"
    assert patch["vector"] == [0.1, 0.2, 0.3]
    assert patch["group_id"] == -1
    assert isinstance(patch["ts"], int)

    memory._narrative_manager.cleanup_group.assert_called_once_with(
        user_id="u1",
        character_id="c1",
        group_id=23,
    )


def test_update_skips_group_cleanup_when_ungrouped() -> None:
    memory = _build_memory()
    memory._store.query.return_value = [
        {
            "id": 8,
            "user_id": "u1",
            "character_id": "c1",
            "memory_type": "episodic",
            "ts": 123,
            "chat_id": "chat-1",
            "text": "old text",
            "group_id": -1,
        }
    ]
    memory._embedding_client.encode.return_value = [[0.4, 0.5]]
    memory._store.update_memory.return_value = True

    ok = memory.update(
        memory_id=8,
        data={"text": "new text"},
        user_id="u1",
        character_id="c1",
    )

    assert ok is True
    memory._narrative_manager.cleanup_group.assert_not_called()


def test_update_returns_false_when_memory_not_found() -> None:
    memory = _build_memory()
    memory._store.query.return_value = []

    ok = memory.update(
        memory_id=999,
        data={"text": "new text"},
        user_id="u1",
        character_id="c1",
    )

    assert ok is False
    memory._embedding_client.encode.assert_not_called()
    memory._store.update_memory.assert_not_called()


def test_update_returns_false_when_embedding_fails() -> None:
    memory = _build_memory()
    memory._store.query.return_value = [
        {
            "id": 9,
            "user_id": "u1",
            "character_id": "c1",
            "memory_type": "episodic",
            "ts": 123,
            "chat_id": "chat-1",
            "text": "old text",
            "group_id": 1,
        }
    ]
    memory._embedding_client.encode.return_value = []

    ok = memory.update(
        memory_id=9,
        data={"text": "new text"},
        user_id="u1",
        character_id="c1",
    )

    assert ok is False
    memory._store.update_memory.assert_not_called()
    memory._narrative_manager.cleanup_group.assert_not_called()


def test_update_returns_false_when_store_update_fails() -> None:
    memory = _build_memory()
    memory._store.query.return_value = [
        {
            "id": 10,
            "user_id": "u1",
            "character_id": "c1",
            "memory_type": "episodic",
            "ts": 123,
            "chat_id": "chat-1",
            "text": "old text",
            "group_id": 3,
        }
    ]
    memory._embedding_client.encode.return_value = [[0.1, 0.9]]
    memory._store.update_memory.return_value = False

    ok = memory.update(
        memory_id=10,
        data={"text": "new text"},
        user_id="u1",
        character_id="c1",
    )

    assert ok is False
    memory._narrative_manager.cleanup_group.assert_not_called()


def test_encode_embeddings_updates_langfuse_usage(monkeypatch) -> None:
    memory = _build_memory()
    memory._config = Mock(embedding_model="qwen/qwen3-embedding-4b")
    embedding_client = Mock()
    embedding_client.encode_with_usage.return_value = EmbeddingResult(
        vectors=[[0.1, 0.2]],
        model="qwen/qwen3-embedding-4b",
        usage_details={"input": 12, "total": 12},
    )
    memory._embedding_client = embedding_client
    observation = Mock()
    client = Mock()
    client.start_observation.return_value = observation

    monkeypatch.setattr("src.memory_system.memory.get_client", lambda: client)

    vectors = memory._encode_embeddings(
        ["hello"],
        user_id="u1",
        character_id="c1",
        chat_id="chat-1",
    )

    assert vectors == [[0.1, 0.2]]
    observation.update.assert_called_once_with(
        output={"count": 1},
        usage_details={"input": 12, "total": 12},
    )
    observation.end.assert_called_once()
