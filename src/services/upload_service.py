"""R2 media upload service."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio

from src.core.config import settings
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.db.models import User
from src.schemas.discover import DiscoverHeroImageUploadResponse
from src.schemas.media import (
    ChatImageUploadResponse,
    CompleteUploadRequest,
    CompleteUploadResponse,
    MediaUploadKind,
    PresignedUploadRequest,
    PresignedUploadResponse,
)
from src.services.image_processing_service import ImageProcessingService
from src.services.local_media_store import LocalMediaStore
from src.services.media_cache_service import MediaCacheService
from src.services.media_sync_worker import MediaSyncWorker
from src.services.media_url_service import (
    avatar_image_key,
    build_avatar_urls,
    build_chat_image_urls,
    build_media_url,
    chat_image_display_object_key,
    discover_hero_object_key,
    original_object_key,
    variant_object_key,
)
from src.services.r2_storage_service import R2StorageService

logger = logging.getLogger(__name__)
CACHE_CONTROL_IMMUTABLE = "public, max-age=31536000, immutable"
DISCOVER_HERO_EXTENSION_BY_CONTENT_TYPE = {
    "image/avif": ".avif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
DISCOVER_HERO_ID_PATTERN = re.compile(r"[^a-z0-9_-]+")


class UploadService:
    """Business/service wrapper around R2 media uploads."""

    def __init__(
        self,
        *,
        r2_storage: R2StorageService,
        media_cache: MediaCacheService,
        image_processor: ImageProcessingService,
        local_store: LocalMediaStore,
        sync_worker: MediaSyncWorker,
    ):
        self._r2_storage = r2_storage
        self._media_cache = media_cache
        self._image_processor = image_processor
        self._local_store = local_store
        self._sync_worker = sync_worker

    async def upload_chat_image(
        self,
        *,
        body: bytes,
        content_type: str | None,
        current_user: User,
    ) -> ChatImageUploadResponse:
        """Stage one chat image locally; R2 receives it in the background."""

        self._require_media_services()

        if not body:
            raise APIError(
                status_code=422,
                code=ErrorCode.VALIDATION_FAILED,
                message="invalid upload: image file is empty",
            )
        if len(body) > settings.MEDIA_MAX_UPLOAD_BYTES:
            limit_mb = max(1, settings.MEDIA_MAX_UPLOAD_BYTES // 1024 // 1024)
            raise APIError(
                status_code=413,
                code=ErrorCode.UPLOAD_FILE_TOO_LARGE,
                message=f"invalid upload: image size exceeds limit; limit_mb={limit_mb}",
            )

        normalized_content_type = (content_type or "").split(";")[0].strip().lower()
        if normalized_content_type not in settings.media_allowed_mime_list:
            allowed = ",".join(settings.media_allowed_mime_list)
            raise APIError(
                status_code=415,
                code=ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
                message=f"invalid upload: file type is not allowed; allowed={allowed}",
            )

        dimensions = await self._image_processor.inspect_image(body)
        image_key = avatar_image_key(
            MediaUploadKind.CHAT_IMAGE, str(current_user.id), uuid.uuid4().hex
        )
        original_key = original_object_key(image_key)
        display_variant = await self._image_processor.build_display_variant(body)
        display_key = chat_image_display_object_key(image_key)

        staged_original = await self._stage_object(
            object_key=original_key,
            body=body,
            content_type=normalized_content_type,
        )
        staged_display = staged_original and await self._stage_object(
            object_key=display_key,
            body=display_variant.body,
            content_type=display_variant.content_type,
        )
        if not (staged_original and staged_display):
            await self._discard_staged(original_key, display_key)
            raise APIError(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="internal error: failed to stage uploaded image",
            )
        self._sync_worker.enqueue(original_key)
        self._sync_worker.enqueue(display_key)
        await self._media_cache.set_cached_media(
            object_key=display_key,
            body=display_variant.body,
            content_type=display_variant.content_type,
            etag=None,
            cache_control=CACHE_CONTROL_IMMUTABLE,
        )
        urls = build_chat_image_urls(image_key)
        logger.info(
            "chat_image staged: image_key=%s original_bytes=%d display_bytes=%d",
            image_key,
            len(body),
            len(display_variant.body),
        )
        return ChatImageUploadResponse(
            image_key=image_key,
            original_url=urls["original"],
            display_url=urls["display"],
            content_type=normalized_content_type,
            size_bytes=len(body),
            width=dimensions.width,
            height=dimensions.height,
        )

    async def _stage_object(self, *, object_key: str, body: bytes, content_type: str) -> bool:
        return await anyio.to_thread.run_sync(
            lambda: self._local_store.put(
                object_key=object_key,
                body=body,
                content_type=content_type,
                cache_control=CACHE_CONTROL_IMMUTABLE,
            )
        )

    async def _discard_staged(self, *object_keys: str) -> None:
        await anyio.to_thread.run_sync(
            lambda: [self._local_store.delete(key) for key in object_keys]
        )

    async def create_presigned_upload(
        self,
        request: PresignedUploadRequest,
        *,
        current_user: User,
    ) -> PresignedUploadResponse:
        self._require_media_services()

        if request.mime_type not in settings.media_allowed_mime_list:
            allowed = ",".join(settings.media_allowed_mime_list)
            raise APIError(
                status_code=415,
                code=ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
                message=f"invalid upload: file type is not allowed; allowed={allowed}",
            )
        if request.size_bytes > settings.MEDIA_MAX_UPLOAD_BYTES:
            limit_mb = max(1, settings.MEDIA_MAX_UPLOAD_BYTES // 1024 // 1024)
            raise APIError(
                status_code=413,
                code=ErrorCode.UPLOAD_FILE_TOO_LARGE,
                message=f"invalid upload: file size exceeds limit; limit_mb={limit_mb}",
            )

        upload_id = uuid.uuid4().hex
        image_id = uuid.uuid4().hex
        image_key = avatar_image_key(request.kind, str(current_user.id), image_id)
        original_key = original_object_key(image_key)
        expires_in = settings.MEDIA_PRESIGNED_URL_TTL_SECONDS
        upload_url = await self._r2_storage.generate_presigned_put_url(
            object_key=original_key,
            content_type=request.mime_type,
            expires_in_seconds=expires_in,
        )
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.MEDIA_UPLOAD_SESSION_TTL_SECONDS)
        await self._media_cache.set_upload_session(
            upload_id,
            {
                "upload_id": upload_id,
                "user_id": str(current_user.id),
                "kind": request.kind.value,
                "image_key": image_key,
                "original_object_key": original_key,
                "mime_type": request.mime_type,
                "size_bytes": request.size_bytes,
                "status": "pending",
                "expires_at": expires_at.isoformat(),
            },
            settings.MEDIA_UPLOAD_SESSION_TTL_SECONDS,
        )
        logger.info(
            "presign: kind=%s image_key=%s mime=%s claimed_size=%d",
            request.kind.value, image_key, request.mime_type, request.size_bytes,
        )
        return PresignedUploadResponse(
            upload_id=upload_id,
            image_key=image_key,
            original_object_key=original_key,
            upload_url=upload_url,
            expires_in_seconds=expires_in,
            required_headers={"Content-Type": request.mime_type},
        )

    async def complete_upload(
        self,
        request: CompleteUploadRequest,
        *,
        current_user: User,
    ) -> CompleteUploadResponse:
        self._require_media_services()

        session = await self._media_cache.get_upload_session(request.upload_id)
        if not session:
            raise APIError(
                status_code=422,
                code=ErrorCode.VALIDATION_FAILED,
                message="validation failed: media upload session is missing or expired",
            )
        if session.get("user_id") != str(current_user.id):
            raise APIError(
                status_code=403,
                code=ErrorCode.FORBIDDEN,
                message="authorization failed: media upload session belongs to another user",
            )
        if session.get("status") == "completed":
            image_key = str(session["image_key"])
            kind = str(session.get("kind", ""))
            if kind == "chat_image":
                urls = build_chat_image_urls(image_key)
                return CompleteUploadResponse(
                    upload_id=request.upload_id,
                    image_key=image_key,
                    avatar_urls=None,
                    original_url=urls["original"],
                    display_url=urls["display"],
                )
            avatar_urls = build_avatar_urls(image_key)
            if avatar_urls is None:
                raise APIError(
                    status_code=409,
                    code=ErrorCode.RESOURCE_CONFLICT,
                    message="resource conflict: completed media upload has no image key",
                )
            return CompleteUploadResponse(
                upload_id=request.upload_id,
                image_key=image_key,
                avatar_urls=avatar_urls,
            )

        locked = await self._media_cache.acquire_upload_lock(request.upload_id)
        if not locked:
            raise APIError(
                status_code=409,
                code=ErrorCode.RESOURCE_CONFLICT,
                message="resource conflict: media upload session is already completing",
            )

        try:
            object_key = str(session["original_object_key"])
            image_key = str(session["image_key"])
            kind = str(session.get("kind", ""))
            original = await self._r2_storage.get_object(object_key=object_key)
            self._assert_etag_matches(request.etag, original.etag)

            if kind == "chat_image":
                display_variant = await self._image_processor.build_display_variant(original.body)
                display_key = chat_image_display_object_key(image_key)
                display_etag = await self._r2_storage.put_object(
                    object_key=display_key,
                    body=display_variant.body,
                    content_type=display_variant.content_type,
                    cache_control=CACHE_CONTROL_IMMUTABLE,
                )
                await self._media_cache.set_cached_media(
                    object_key=display_key,
                    body=display_variant.body,
                    content_type=display_variant.content_type,
                    etag=display_etag,
                    cache_control=CACHE_CONTROL_IMMUTABLE,
                )
                await self._media_cache.mark_upload_completed(
                    request.upload_id,
                    session,
                    settings.MEDIA_UPLOAD_SESSION_TTL_SECONDS,
                )
                urls = build_chat_image_urls(image_key)
                logger.info(
                    "chat_image complete: image_key=%s original_bytes=%d display_bytes=%d",
                    image_key,
                    len(original.body),
                    len(display_variant.body),
                )
                return CompleteUploadResponse(
                    upload_id=request.upload_id,
                    image_key=image_key,
                    avatar_urls=None,
                    original_url=urls["original"],
                    display_url=urls["display"],
                )

            variants = await self._image_processor.build_avatar_variants(original.body)
            for variant in variants:
                object_key = variant_object_key(image_key, variant.size)
                etag = await self._r2_storage.put_object(
                    object_key=object_key,
                    body=variant.body,
                    content_type=variant.content_type,
                    cache_control=CACHE_CONTROL_IMMUTABLE,
                )
                await self._media_cache.set_cached_media(
                    object_key=object_key,
                    body=variant.body,
                    content_type=variant.content_type,
                    etag=etag,
                    cache_control=CACHE_CONTROL_IMMUTABLE,
                )

            await self._media_cache.mark_upload_completed(
                request.upload_id,
                session,
                settings.MEDIA_UPLOAD_SESSION_TTL_SECONDS,
            )
            avatar_urls = build_avatar_urls(image_key)
            if avatar_urls is None:
                raise APIError(
                    status_code=409,
                    code=ErrorCode.RESOURCE_CONFLICT,
                    message="resource conflict: media upload has no image key",
                )
            logger.info(
                "Completed media upload",
                extra={
                    "event": "media.upload_completed",
                    "user_id": str(current_user.id),
                    "operation": "media_complete",
                },
            )
            return CompleteUploadResponse(
                upload_id=request.upload_id,
                image_key=image_key,
                avatar_urls=avatar_urls,
            )
        finally:
            await self._media_cache.release_upload_lock(request.upload_id)

    async def upload_discover_hero_image(
        self,
        *,
        body: bytes,
        filename: str | None,
        content_type: str | None,
        key_hint: str | None,
    ) -> DiscoverHeroImageUploadResponse:
        self._require_media_services()

        if not body:
            raise APIError(
                status_code=422,
                code=ErrorCode.VALIDATION_FAILED,
                message="validation failed: uploaded file is empty",
            )

        normalized_content_type = (content_type or "").split(";")[0].strip().lower()
        allowed_content_types = sorted(
            set(settings.media_allowed_mime_list) & set(DISCOVER_HERO_EXTENSION_BY_CONTENT_TYPE)
        )
        if normalized_content_type not in allowed_content_types:
            allowed = ",".join(allowed_content_types)
            raise APIError(
                status_code=415,
                code=ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
                message=f"invalid upload: file type is not allowed; allowed={allowed}",
            )
        if len(body) > settings.MEDIA_MAX_UPLOAD_BYTES:
            limit_mb = max(1, settings.MEDIA_MAX_UPLOAD_BYTES // 1024 // 1024)
            raise APIError(
                status_code=413,
                code=ErrorCode.UPLOAD_FILE_TOO_LARGE,
                message=f"invalid upload: file size exceeds limit; limit_mb={limit_mb}",
            )

        dimensions = await self._image_processor.inspect_image(body)
        image_id = self._discover_hero_image_id(key_hint=key_hint, filename=filename)
        object_key = discover_hero_object_key(image_id, ".avif")

        avif_body = await self._image_processor.convert_to_avif(body)
        etag = await self._r2_storage.put_object(
            object_key=object_key,
            body=avif_body,
            content_type="image/avif",
            cache_control=CACHE_CONTROL_IMMUTABLE,
        )
        await self._media_cache.set_cached_media(
            object_key=object_key,
            body=avif_body,
            content_type="image/avif",
            etag=etag,
            cache_control=CACHE_CONTROL_IMMUTABLE,
        )

        logger.info(
            "Uploaded discover hero image",
            extra={
                "event": "media.discover_hero_uploaded",
                "operation": "discover_hero_upload",
                "object_key": object_key,
                "size_bytes": len(avif_body),
            },
        )
        return DiscoverHeroImageUploadResponse(
            image_key=object_key,
            image_url=build_media_url(object_key),
            object_key=object_key,
            content_type="image/avif",
            size_bytes=len(avif_body),
            width=dimensions.width,
            height=dimensions.height,
        )

    def _require_media_services(self) -> None:
        if not self._r2_storage or not self._media_cache or not self._image_processor:
            raise APIError(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="internal error: media upload service is not configured",
            )

    def _assert_etag_matches(self, submitted_etag: str | None, stored_etag: str | None) -> None:
        if not submitted_etag or not stored_etag:
            return
        normalized_submitted = submitted_etag.strip().strip('"')
        normalized_stored = stored_etag.strip().strip('"')
        if normalized_submitted and normalized_stored and normalized_submitted != normalized_stored:
            raise APIError(
                status_code=409,
                code=ErrorCode.RESOURCE_CONFLICT,
                message="resource conflict: uploaded object etag does not match completion request",
            )

    def _discover_hero_image_id(self, *, key_hint: str | None, filename: str | None) -> str:
        raw = key_hint or Path(filename or "").stem or uuid.uuid4().hex
        stem = Path(raw).stem.lower().strip()
        normalized = DISCOVER_HERO_ID_PATTERN.sub("-", stem)
        normalized = re.sub(r"-+", "-", normalized).strip("-_")
        return normalized[:80] or uuid.uuid4().hex
