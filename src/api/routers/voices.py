"""Voice profile management router."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi import status as http_status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from src.api import deps
from src.api.response import no_content_response, success_response
from src.core.dependencies import get_voice_profile_service, require_voice_clone_access
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.voice_profile import (
    VoiceCatalogResponse,
    VoiceCloneAccepted,
    VoiceProfileResponse,
    VoiceProfilesPageResponse,
    VoiceProfileUpdate,
    VoiceSourceTypeEnum,
    VoiceStatusEnum,
)
from src.services.voice_profile_service import VoiceProfileService

router = APIRouter(prefix="/v1/voices", tags=["voices"])


@router.get("", response_model=SuccessEnvelope[VoiceProfilesPageResponse])
async def list_my_voices(
    status: VoiceStatusEnum | None = Query(None),
    source_type: VoiceSourceTypeEnum | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(deps.get_current_user),
    service: VoiceProfileService = Depends(get_voice_profile_service),
) -> JSONResponse:
    data = await service.list_my_voices(
        current_user=current_user,
        status=status.value if status else None,
        source_type=source_type.value if source_type else None,
        cursor=cursor,
        limit=limit,
    )
    return success_response(data, status_code=200)


@router.get("/catalog", response_model=SuccessEnvelope[VoiceCatalogResponse])
async def list_selectable_voices(
    provider: str | None = Query(None),
    model: str | None = Query(None),
    include_system: bool = Query(True),
    include_user_custom: bool = Query(True),
    current_user: User = Depends(deps.get_current_user),
    service: VoiceProfileService = Depends(get_voice_profile_service),
) -> JSONResponse:
    data = await service.list_catalog(
        current_user=current_user,
        provider=provider,
        model=model,
        include_system=include_system,
        include_user_custom=include_user_custom,
    )
    return success_response(data, status_code=200)


@router.post(
    "/clones",
    response_model=SuccessEnvelope[VoiceCloneAccepted],
    status_code=http_status.HTTP_202_ACCEPTED,
)
async def create_voice_clone(
    provider: str = Form("dashscope"),
    display_name: str = Form(...),
    preview_text: str = Form(...),
    source_audio: UploadFile = File(...),
    source_audio_format: Literal["wav", "mp3", "m4a", "flac", "pcm", "opus"] = Form("wav"),
    description: str | None = Form(None),
    avatar_image_key: str | None = Form(None),
    language_hint: str | None = Form(None),
    idempotency_key: str | None = Form(None),
    current_user: User = Depends(require_voice_clone_access),
    service: VoiceProfileService = Depends(get_voice_profile_service),
) -> JSONResponse:
    payload = await source_audio.read()
    data = await service.create_clone(
        current_user=current_user,
        provider=provider,
        display_name=display_name,
        preview_text=preview_text,
        source_audio_bytes=payload,
        source_audio_format=source_audio_format,
        description=description,
        avatar_image_key=avatar_image_key,
        language_hint=language_hint,
        idempotency_key=idempotency_key,
    )
    return success_response(data, status_code=202, message="accepted")


@router.get("/{voice_id}/preview/audio")
async def stream_voice_preview_audio(
    voice_id: uuid.UUID,
    audio_format: Literal["opus", "mp3"] = Query("mp3"),
    current_user: User = Depends(deps.get_current_user),
    service: VoiceProfileService = Depends(get_voice_profile_service),
) -> StreamingResponse:
    mime_type, stream = await service.get_preview_audio_stream(
        voice_id=voice_id,
        current_user=current_user,
        audio_format=audio_format,
    )
    return StreamingResponse(
        stream,
        media_type=mime_type,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{voice_id}", response_model=SuccessEnvelope[VoiceProfileResponse])
async def get_voice_by_id(
    voice_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: VoiceProfileService = Depends(get_voice_profile_service),
) -> JSONResponse:
    data = await service.get_voice_by_id(voice_id=voice_id, current_user=current_user)
    return success_response(data, status_code=200)


@router.patch("/{voice_id}", response_model=SuccessEnvelope[VoiceProfileResponse])
async def patch_voice_by_id(
    voice_id: uuid.UUID,
    patch: VoiceProfileUpdate,
    current_user: User = Depends(deps.get_current_user),
    service: VoiceProfileService = Depends(get_voice_profile_service),
) -> JSONResponse:
    patch_data = patch.model_dump(exclude_unset=True)
    data = await service.patch_voice_by_id(
        voice_id=voice_id,
        current_user=current_user,
        patch_data=patch_data,
    )
    return success_response(data, status_code=200)


@router.delete("/{voice_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_voice_by_id(
    voice_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: VoiceProfileService = Depends(get_voice_profile_service),
) -> Response:
    await service.delete_voice_by_id(voice_id=voice_id, current_user=current_user)
    return no_content_response()
