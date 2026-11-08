"""Voice preview store: session-scoped handles, ownership, and lookup errors."""

from __future__ import annotations

import json
import uuid

import pytest

from src.core.exceptions import APIError
from src.services.forge_preview_store import (
    ForgePreviewStore,
    normalize_preview_handle,
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


async def test_serial_increases_within_a_session_and_restarts_per_session():
    store, _ = _store()
    user_id = uuid.uuid4()
    first_session = uuid.uuid4()
    second_session = uuid.uuid4()

    assert (
        await store.create(session_id=first_session, user_id=user_id, payload={})
        == "pv1"
    )
    assert (
        await store.create(session_id=first_session, user_id=user_id, payload={})
        == "pv2"
    )
    assert (
        await store.create(session_id=second_session, user_id=user_id, payload={})
        == "pv1"
    )


async def test_get_returns_payload_for_the_owner():
    store, _ = _store()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    payload = {"base_voice_id": "Kai", "voice_profile": {"volume": 50}}

    handle = await store.create(
        session_id=session_id, user_id=user_id, payload=payload
    )
    loaded = await store.get(
        session_id=session_id, handle=handle, user_id=user_id
    )

    assert loaded == payload


async def test_get_accepts_a_handle_written_with_loose_formatting():
    store, _ = _store()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await store.create(
        session_id=session_id, user_id=user_id, payload={"base_voice_id": "Kai"}
    )

    loaded = await store.get(
        session_id=session_id, handle=" PV 01 ", user_id=user_id
    )

    assert loaded == {"base_voice_id": "Kai"}


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


async def test_get_reports_a_missing_preview():
    store, _ = _store()
    session_id = uuid.uuid4()

    with pytest.raises(APIError) as excinfo:
        await store.get(
            session_id=session_id, handle="pv9", user_id=uuid.uuid4()
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "validation_failed"


async def test_get_rejects_a_handle_that_is_not_a_serial():
    store, _ = _store()

    with pytest.raises(APIError) as excinfo:
        await store.get(
            session_id=uuid.uuid4(), handle="not-a-handle", user_id=uuid.uuid4()
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "validation_failed"


def test_normalize_preview_handle_folds_loose_writing():
    assert normalize_preview_handle("pv3") == "pv3"
    assert normalize_preview_handle(" PV 3 ") == "pv3"
    assert normalize_preview_handle("pv003") == "pv3"
    assert normalize_preview_handle("") is None
    assert normalize_preview_handle("pv") is None
    assert normalize_preview_handle("preview") is None
    assert normalize_preview_handle("pv3x") is None
