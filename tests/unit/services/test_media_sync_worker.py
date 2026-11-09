"""Background mirroring of staged media into R2."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.core.config import settings
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.services.local_media_store import LocalMediaStore
from src.services.media_sync_worker import MediaSyncWorker


class _FakeR2:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.puts: list[dict[str, object]] = []
        self.fail_times = fail_times

    async def put_object(
        self,
        *,
        object_key: str,
        body: bytes,
        content_type: str,
        cache_control: str,
    ) -> str | None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to write media object",
            )
        self.puts.append(
            {
                "object_key": object_key,
                "body": body,
                "content_type": content_type,
                "cache_control": cache_control,
            }
        )
        return '"etag"'


def _worker(tmp_path: Path, r2: _FakeR2, *, retry_scan_seconds: int = 60) -> MediaSyncWorker:
    config = settings.model_copy(
        update={
            "LOCAL_MEDIA_SYNC_ENABLED": True,
            "LOCAL_MEDIA_SYNC_WORKER_COUNT": 1,
            "LOCAL_MEDIA_SYNC_SCAN_SECONDS": retry_scan_seconds,
        }
    )
    return MediaSyncWorker(
        local_store=LocalMediaStore(tmp_path),
        r2_storage=r2,  # type: ignore[arg-type]
        config=config,
    )


async def test_sync_uploads_bytes_then_marks_the_object_synced(tmp_path: Path) -> None:
    store = LocalMediaStore(tmp_path)
    key = "images/avatars/chat/user-1/image-1/display.avif"
    store.put(
        object_key=key,
        body=b"avif-bytes",
        content_type="image/avif",
        cache_control="public, max-age=31536000, immutable",
    )
    r2 = _FakeR2()
    worker = _worker(tmp_path, r2)

    assert await worker._sync_one(key) is True

    assert r2.puts == [
        {
            "object_key": key,
            "body": b"avif-bytes",
            "content_type": "image/avif",
            "cache_control": "public, max-age=31536000, immutable",
        }
    ]
    assert store.pending_keys() == []


async def test_failed_upload_keeps_the_object_pending_for_a_later_rescan(tmp_path: Path) -> None:
    store = LocalMediaStore(tmp_path)
    key = "audios/chat/user-1/a.wav"
    store.put(object_key=key, body=b"wav", content_type="audio/wav", cache_control="private")
    r2 = _FakeR2(fail_times=1)
    worker = _worker(tmp_path, r2)

    assert await worker._sync_one(key) is False
    assert store.pending_keys() == [key]

    assert await worker._sync_one(key) is True
    assert store.pending_keys() == []


async def test_start_mirrors_objects_left_over_from_a_previous_run(tmp_path: Path) -> None:
    store = LocalMediaStore(tmp_path)
    key = "images/avatars/chat/user-1/image-1/original"
    store.put(object_key=key, body=b"jpeg", content_type="image/jpeg", cache_control="immutable")
    r2 = _FakeR2()
    worker = _worker(tmp_path, r2)

    await worker.start()
    try:
        for _ in range(50):
            if not store.pending_keys():
                break
            await asyncio.sleep(0.02)
    finally:
        await worker.stop()

    assert store.pending_keys() == []
    assert [put["object_key"] for put in r2.puts] == [key]
