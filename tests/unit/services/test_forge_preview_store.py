"""Voice preview store: session-scoped handles, ownership, and lookup errors."""

from __future__ import annotations

import json
import uuid

import pytest

from src.core.exceptions import APIError
from src.services.forge_preview_store import (
    ForgePreviewStore,
)


class _FakePipeline:
    def __init__(self, client: _FakeRedis) -> None:
        self._client = client
        self._ops: list[tuple[str, str, object]] = []

    def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self._ops.append(("set", key, value))

    def sadd(self, key: str, member: str) -> None:
        self._ops.append(("sadd", key, member))

    def expire(self, key: str, ttl: int) -> None:
        self._ops.append(("expire", key, ttl))

    async def execute(self) -> None:
        for op, key, value in self._ops:
            if op == "set":
                self._client.values[key] = value  # type: ignore[assignment]
            elif op == "sadd":
                self._client.sets.setdefault(key, set()).add(value)  # type: ignore[arg-type]


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.sets: dict[str, set[str]] = {}
        self.counters: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)


def _store() -> tuple[ForgePreviewStore, _FakeRedis]:
    store = ForgePreviewStore()
    client = _FakeRedis()
    store._client = client  # type: ignore[assignment]
    return store, client


async def test_create_returns_session_scoped_serial_and_stores_payload():
    store, client = _store()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()

    handle = await store.create(
        session_id=session_id,
        user_id=user_id,
        payload={"base_voice_id": "Eldric Sage", "sample_text": "hello"},
    )

    assert handle == "pv1"
    stored = client.values[f"forge:preview:{session_id}:pv1"]
    assert json.loads(stored) == {
        "user_id": str(user_id),
        "payload": {"base_voice_id": "Eldric Sage", "sample_text": "hello"},
    }
    assert client.sets[f"forge:preview:index:{session_id}"] == {"pv1"}




async def test_get_rejects_a_preview_owned_by_another_user():
    store, _ = _store()
    session_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    handle = await store.create(
        session_id=session_id, user_id=owner_id, payload={"base_voice_id": "Kai"}
    )

    with pytest.raises(APIError) as excinfo:
        await store.get(
            session_id=session_id, handle=handle, user_id=uuid.uuid4()
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.code == "forbidden"


