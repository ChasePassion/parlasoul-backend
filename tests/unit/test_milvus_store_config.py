from __future__ import annotations

from src.memory_system.clients.milvus_store import MilvusStore


class _StubMilvusClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def test_groups_collection_name_is_configurable(monkeypatch) -> None:
    created_clients: list[_StubMilvusClient] = []

    def _client_factory(**kwargs):
        client = _StubMilvusClient(**kwargs)
        created_clients.append(client)
        return client

    monkeypatch.setattr("src.memory_system.clients.milvus_store.MilvusClient", _client_factory)

    store = MilvusStore(
        uri="https://milvus.example.com",
        token="token",
        collection_name="memories_dev",
        groups_collection_name="groups_dev",
    )

    assert created_clients[0].kwargs == {
        "uri": "https://milvus.example.com",
        "token": "token",
    }
    assert store._get_groups_collection_name() == "groups_dev"
