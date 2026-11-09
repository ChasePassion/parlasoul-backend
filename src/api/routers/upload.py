"""R2 media upload and read router."""

from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, File, Form, Response, UploadFile

from src.api import deps
from src.api.response import success_response
from src.core.dependencies import (
    get_audio_media_service,
    get_local_media_store_provider,
    get_media_cache_service_provider,
    get_r2_storage_service_provider,
    get_upload_service,
)
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.discover import DiscoverHeroImageUploadResponse
from src.schemas.media import (
    ChatAudioUploadResponse,
    ChatImageUploadResponse,
    CompleteUploadRequest,
    CompleteUploadResponse,
    PresignedUploadRequest,
    PresignedUploadResponse,
)
from src.services.audio_media_service import AudioMediaService
from src.services.local_media_store import LocalMediaStore
from src.services.media_cache_service import MediaCacheService
from src.services.media_url_service import (
    public_media_content_type,
    validate_public_media_object_key,
)
from src.services.r2_storage_service import R2StorageService
from src.services.upload_service import UploadService

router = APIRouter(tags=["upload"])
MEDIA_CACHE_CONTROL = "public, max-age=31536000, immutable"
_LOCAL_MEDIA_ROOT = Path("static") / "media"


@router.post("/v1/uploads/presign", response_model=SuccessEnvelope[PresignedUploadResponse])
async def presign_upload(
    request: PresignedUploadRequest,
    current_user: User = Depends(deps.get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> SuccessEnvelope[PresignedUploadResponse]:
    data = await service.create_presigned_upload(request, current_user=current_user)
    return success_response(data, status_code=200)


@router.post("/v1/uploads/complete", response_model=SuccessEnvelope[CompleteUploadResponse])
async def complete_upload(
    request: CompleteUploadRequest,
    current_user: User = Depends(deps.get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> SuccessEnvelope[CompleteUploadResponse]:
    data = await service.complete_upload(request, current_user=current_user)
    return success_response(data, status_code=200)


@router.post("/v1/uploads/chat-image", response_model=SuccessEnvelope[ChatImageUploadResponse])
async def upload_chat_image(
    file: UploadFile = File(...),
    current_user: User = Depends(deps.get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> SuccessEnvelope[ChatImageUploadResponse]:
    data = await service.upload_chat_image(
        body=await file.read(),
        content_type=file.content_type,
        current_user=current_user,
    )
    return success_response(data, status_code=200)


@router.post("/v1/uploads/audio", response_model=SuccessEnvelope[ChatAudioUploadResponse])
async def upload_chat_audio(
    file: UploadFile = File(...),
    current_user: User = Depends(deps.get_current_user),
    service: AudioMediaService = Depends(get_audio_media_service),
) -> SuccessEnvelope[ChatAudioUploadResponse]:
    data = await service.store_chat_audio(
        owner_id=current_user.id,
        body=await file.read(),
        filename=file.filename,
        content_type=file.content_type,
    )
    return success_response(data, status_code=200)


@router.post(
    "/v1/internal/discover/hero-image",
    response_model=SuccessEnvelope[DiscoverHeroImageUploadResponse],
)
async def upload_discover_hero_image(
    file: UploadFile = File(...),
    key_hint: str | None = Form(default=None),
    secret: str = Form(..., description="Internal secret password"),
    service: UploadService = Depends(get_upload_service),
) -> SuccessEnvelope[DiscoverHeroImageUploadResponse]:
    if secret != "666666":
        raise APIError(
            status_code=401,
            code="auth_invalid",
            message="invalid secret",
        )
    body = await file.read()
    data = await service.upload_discover_hero_image(
        body=body,
        filename=file.filename,
        content_type=file.content_type,
        key_hint=key_hint,
    )
    return success_response(data, status_code=200)


@router.get("/media/{object_key:path}")
async def get_media_object(
    object_key: str,
    r2_storage: R2StorageService = Depends(get_r2_storage_service_provider),
    media_cache: MediaCacheService = Depends(get_media_cache_service_provider),
    local_store: LocalMediaStore = Depends(get_local_media_store_provider),
) -> Response:
    normalized_key = validate_public_media_object_key(object_key)
    if normalized_key is None:
        raise APIError(
            status_code=404,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found: media object does not exist",
        )

    local_body = _read_local_media_object(normalized_key)
    if local_body is not None:
        return _media_response(
            body=local_body,
            content_type=public_media_content_type(normalized_key),
            etag=None,
            cache_control=MEDIA_CACHE_CONTROL,
            cache_state="local",
        )

    # Fresh uploads live on this host until the background sync reaches R2, so
    # they must be served before the negative cache can mark them missing.
    staged = await anyio.to_thread.run_sync(local_store.get, normalized_key)
    if staged is not None:
        return _media_response(
            body=staged.body,
            content_type=staged.content_type or public_media_content_type(normalized_key),
            etag=None,
            cache_control=staged.cache_control or MEDIA_CACHE_CONTROL,
            cache_state="local",
        )

    if await media_cache.has_negative_media(normalized_key):
        raise APIError(
            status_code=404,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found: media object does not exist",
        )

    cached = await media_cache.get_cached_media(normalized_key)
    if cached:
        return _media_response(
            body=cached.body,
            content_type=cached.content_type,
            etag=cached.etag,
            cache_control=cached.cache_control,
            cache_state="hit",
        )

    try:
        media_object = await r2_storage.get_object(object_key=normalized_key)
    except APIError as exc:
        if exc.status_code == 404:
            await media_cache.set_negative_media(normalized_key)
        raise

    content_type = media_object.content_type or public_media_content_type(normalized_key)
    cache_control = media_object.cache_control or MEDIA_CACHE_CONTROL
    await media_cache.set_cached_media(
        object_key=normalized_key,
        body=media_object.body,
        content_type=content_type,
        etag=media_object.etag,
        cache_control=cache_control,
    )
    return _media_response(
        body=media_object.body,
        content_type=content_type,
        etag=media_object.etag,
        cache_control=cache_control,
        cache_state="miss",
    )

def _media_response(
    *,
    body: bytes,
    content_type: str,
    etag: str | None,
    cache_control: str,
    cache_state: str,
) -> Response:
    headers = {
        "Cache-Control": cache_control,
        "X-Media-Cache": cache_state,
    }
    if etag:
        headers["ETag"] = etag
    return Response(content=body, media_type=content_type, headers=headers)


def _read_local_media_object(object_key: str) -> bytes | None:
    if not object_key.startswith("voices/preview/"):
        return None
    root = _LOCAL_MEDIA_ROOT.resolve()
    candidate = (root / object_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate.read_bytes()
