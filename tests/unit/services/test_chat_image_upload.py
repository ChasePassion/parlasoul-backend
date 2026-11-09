"""Chat image upload stages locally and never waits for R2."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from src.services.image_processing_service import ImageDimensions, ImageVariant
from src.services.local_media_store import LocalMediaStore
from src.services.media_url_service import (
    build_chat_image_urls,
    chat_image_display_object_key,
    original_object_key,
)
from src.services.upload_service import UploadService


class _FakeImageProcessor:
    async def inspect_image(self, original_bytes: bytes) -> ImageDimensions:
        assert original_bytes
        return ImageDimensions(width=1920, height=1080)

    async def build_display_variant(self, original_bytes: bytes) -> ImageVariant:
        assert original_bytes
        return ImageVariant(size=1024, body=b"display-avif")


class _FakeMediaCache:
    def __init__(self) -> None:
        self.cached: list[str] = []

    async def set_cached_media(self, *, object_key: str, body: bytes, **_: object) -> None:
        self.cached.append(object_key)


class _RecordingSyncWorker:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, object_key: str) -> None:
        self.enqueued.append(object_key)


class _ForbiddenR2:
    async def put_object(self, **_: object) -> str | None:
        raise AssertionError("chat image upload must not touch R2 inside the request")

    async def get_object(self, **_: object) -> object:
        raise AssertionError("chat image upload must not touch R2 inside the request")


def _service(tmp_path: Path, cache: _FakeMediaCache, sync: _RecordingSyncWorker) -> UploadService:
    return UploadService(
        r2_storage=_ForbiddenR2(),  # type: ignore[arg-type]
        media_cache=cache,  # type: ignore[arg-type]
        image_processor=_FakeImageProcessor(),  # type: ignore[arg-type]
        local_store=LocalMediaStore(tmp_path),
        sync_worker=sync,  # type: ignore[arg-type]
    )


async def test_upload_returns_urls_while_both_objects_are_staged_locally(
    tmp_path: Path,
) -> None:
    cache = _FakeMediaCache()
    sync = _RecordingSyncWorker()
    service = _service(tmp_path, cache, sync)
    user = SimpleNamespace(id=uuid.uuid4())

    response = await service.upload_chat_image(
        body=b"jpeg-original",
        content_type="image/jpeg",
        current_user=user,
    )

    assert response.width == 1920
    assert response.height == 1080
    assert response.content_type == "image/jpeg"
    assert response.size_bytes == len(b"jpeg-original")

    urls = build_chat_image_urls(response.image_key)
    assert response.original_url == urls["original"]
    assert response.display_url == urls["display"]

    store = LocalMediaStore(tmp_path)
    original = store.get(original_object_key(response.image_key))
    display = store.get(chat_image_display_object_key(response.image_key))
    assert original is not None and original.body == b"jpeg-original"
    assert original.content_type == "image/jpeg"
    assert display is not None and display.body == b"display-avif"

    assert sync.enqueued == [
        original_object_key(response.image_key),
        chat_image_display_object_key(response.image_key),
    ]
    assert cache.cached == [chat_image_display_object_key(response.image_key)]
    assert store.pending_keys()
