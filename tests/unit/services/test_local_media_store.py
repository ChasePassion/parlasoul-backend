"""Local media staging: atomic writes, pending scan and garbage collection."""

from __future__ import annotations

from pathlib import Path

from src.services.local_media_store import LocalMediaStore


def _store(tmp_path: Path) -> LocalMediaStore:
    return LocalMediaStore(tmp_path)


def test_round_trip_keeps_bytes_and_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = "images/avatars/chat/user-1/image-1/display.avif"

    assert store.put(
        object_key=key,
        body=b"avif-bytes",
        content_type="image/avif",
        cache_control="public, max-age=31536000, immutable",
    )

    stored = store.get(key)
    assert stored is not None
    assert stored.body == b"avif-bytes"
    assert stored.content_type == "image/avif"
    assert stored.cache_control == "public, max-age=31536000, immutable"


def test_put_rejects_keys_escaping_the_root(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert not store.put(
        object_key="../outside.bin",
        body=b"x",
        content_type="application/octet-stream",
        cache_control="no-store",
    )
    assert store.get("../outside.bin") is None


def test_pending_keys_tracks_sync_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = "audios/chat/user-1/a.wav"
    second = "audios/chat/user-1/b.wav"
    for key in (first, second):
        store.put(object_key=key, body=b"x", content_type="audio/wav", cache_control="no-store")

    assert sorted(store.pending_keys()) == sorted([first, second])

    store.mark_synced(first)

    assert store.pending_keys() == [second]


def test_missing_object_is_not_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = "images/avatars/chat/user-1/image-1/original"
    store.put(object_key=key, body=b"x", content_type="image/jpeg", cache_control="no-store")

    store.delete(key)

    assert store.pending_keys() == []


def test_collect_garbage_removes_only_old_synced_objects(tmp_path: Path) -> None:
    store = _store(tmp_path)
    synced = "images/avatars/chat/user-1/image-1/display.avif"
    pending = "images/avatars/chat/user-2/image-2/display.avif"
    for key in (synced, pending):
        store.put(object_key=key, body=b"x", content_type="image/avif", cache_control="no-store")
    store.mark_synced(synced)

    removed = store.collect_garbage(retention_hours=-1)

    assert removed == 1
    assert store.get(synced) is None
    assert store.get(pending) is not None
