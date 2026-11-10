from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import upload
from src.core.dependencies import (
    get_local_media_store_provider,
    get_media_cache_service_provider,
    get_r2_storage_service_provider,
    get_upload_service,
)
from src.db.models import User
from src.schemas.discover import DiscoverHeroImageUploadResponse
from src.schemas.media import (
    AvatarUrls,
    ChatImageUploadResponse,
    CompleteUploadResponse,
    PresignedUploadResponse,
)
from src.services.local_media_store import LocalMediaObject
from src.services.media_cache_service import CachedMediaObject


class _StubUploadService:
    def __init__(self) -> None:
        self.create_presigned_upload = AsyncMock(
            return_value=PresignedUploadResponse(
                upload_id="upload-1",
                image_key="images/avatars/characters/user-1/image-1",
                original_object_key="images/avatars/characters/user-1/image-1/original",
                upload_url="https://example.com/upload",
                expires_in_seconds=900,
                required_headers={"Content-Type": "image/jpeg"},
            )
        )
        self.complete_upload = AsyncMock(
            return_value=CompleteUploadResponse(
                upload_id="upload-1",
                image_key="images/avatars/characters/user-1/image-1",
                avatar_urls=AvatarUrls(
                    sm="/media/images/avatars/characters/user-1/image-1/96.avif",
                    md="/media/images/avatars/characters/user-1/image-1/192.avif",
                    lg="/media/images/avatars/characters/user-1/image-1/512.avif",
                    xl="/media/images/avatars/characters/user-1/image-1/512.avif",
                ),
            )
        )
        self.upload_discover_hero_image = AsyncMock(
            return_value=DiscoverHeroImageUploadResponse(
                image_key="images/discover/heroes/elon-21-9.jpg",
                image_url="/media/images/discover/heroes/elon-21-9.jpg",
                object_key="images/discover/heroes/elon-21-9.jpg",
                content_type="image/jpeg",
                size_bytes=12,
                width=2100,
                height=900,
            )
        )
        self.upload_chat_image = AsyncMock(
            return_value=ChatImageUploadResponse(
                image_key="images/avatars/chat/user-1/image-1",
                original_url="/media/images/avatars/chat/user-1/image-1/original",
                display_url="/media/images/avatars/chat/user-1/image-1/display.avif",
                content_type="image/jpeg",
                size_bytes=10,
                width=1920,
                height=1080,
            )
        )


class _StubMediaCache:
    def __init__(self) -> None:
        self.get_cached_media = AsyncMock(
            return_value=CachedMediaObject(
                body=b"avif-bytes",
                content_type="image/avif",
                etag='"etag-1"',
                cache_control="public, max-age=31536000, immutable",
            )
        )
        self.has_negative_media = AsyncMock(return_value=False)
        self.set_negative_media = AsyncMock()
        self.set_cached_media = AsyncMock()


def _create_app(
    service: _StubUploadService,
    local_store: MagicMock | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(upload.router)
    app.dependency_overrides[get_upload_service] = lambda: service
    store = local_store or MagicMock(get=MagicMock(return_value=None))
    app.dependency_overrides[get_local_media_store_provider] = lambda: store

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="u@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    return app




def test_media_route_serves_a_locally_staged_object_before_r2() -> None:
    service = _StubUploadService()
    cache = _StubMediaCache()
    store = MagicMock(
        get=MagicMock(
            return_value=LocalMediaObject(
                body=b"staged-jpeg",
                content_type="image/jpeg",
                cache_control="public, max-age=31536000, immutable",
            )
        )
    )
    app = _create_app(service, local_store=store)
    app.dependency_overrides[get_media_cache_service_provider] = lambda: cache
    app.dependency_overrides[get_r2_storage_service_provider] = lambda: object()

    with TestClient(app) as client:
        resp = client.get("/media/images/avatars/chat/user-1/image-1/original")

    assert resp.status_code == 200
    assert resp.content == b"staged-jpeg"
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.headers["x-media-cache"] == "local"
    cache.get_cached_media.assert_not_awaited()




def test_internal_discover_hero_upload_route_delegates_to_service() -> None:
    service = _StubUploadService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/internal/discover/hero-image",
            data={"key_hint": "elon-21-9", "secret": "666666"},
            files={"file": ("Elon-21-9.jpg", b"image-bytes", "image/jpeg")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["image_key"] == "images/discover/heroes/elon-21-9.jpg"
    service.upload_discover_hero_image.assert_awaited_once()


@pytest.mark.parametrize(
    ("key", "cached", "expected_type", "assert_cache_header"),
    [
        (
            "/media/images/avatars/characters/user-1/image-1/192.avif",
            CachedMediaObject(
                body=b"avif-bytes",
                content_type="image/avif",
                etag='"etag-1"',
                cache_control="public, max-age=31536000, immutable",
            ),
            "image/avif",
            True,
        ),
        (
            "/media/images/discover/heroes/elon-21-9.jpg",
            CachedMediaObject(
                body=b"jpg-bytes",
                content_type="image/jpeg",
                etag='"etag-2"',
                cache_control="public, max-age=31536000, immutable",
            ),
            "image/jpeg",
            False,
        ),
    ],
    ids=["avatar_avif", "discover_hero_jpg"],
)
def test_media_route_serves_cached_object(
    key: str, cached: CachedMediaObject, expected_type: str, assert_cache_header: bool
) -> None:
    service = _StubUploadService()
    cache = _StubMediaCache()
    cache.get_cached_media.return_value = cached
    app = _create_app(service)
    app.dependency_overrides[get_media_cache_service_provider] = lambda: cache
    app.dependency_overrides[get_r2_storage_service_provider] = lambda: object()

    with TestClient(app) as client:
        resp = client.get(key)

    assert resp.status_code == 200
    assert resp.content == cached.body
    assert resp.headers["content-type"].startswith(expected_type)
    if assert_cache_header:
        assert resp.headers["x-media-cache"] == "hit"
    cache.get_cached_media.assert_awaited_once()


def test_media_route_rejects_non_public_object_key() -> None:
    service = _StubUploadService()
    app = _create_app(service)

    with TestClient(app) as client:
        resp = client.get("/media/private/object.txt")

    assert resp.status_code == 404
