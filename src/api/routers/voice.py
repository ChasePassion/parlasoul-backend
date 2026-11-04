"""Voice APIs for STT transcription and TTS playback."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse

from src.api import deps
from src.api.response import success_response
from src.core.config import settings
from src.core.dependencies import get_voice_service
from src.core.exceptions import APIError
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.voice import STTTranscriptionResult
from src.services.voice_service import VoiceService

router = APIRouter(prefix="/v1/voice", tags=["voice"])


@router.post("/stt/transcriptions", response_model=SuccessEnvelope[STTTranscriptionResult])
async def create_stt_transcription(
    audio: UploadFile = File(...),
    audio_format: Literal["pcm", "wav", "opus", "speex", "aac", "amr"] = Form("wav"),
    sample_rate: int = Form(16000),
    current_user: User = Depends(deps.get_current_user),
    service: VoiceService = Depends(get_voice_service),
) -> SuccessEnvelope[STTTranscriptionResult]:
    # Bind auth context for consistency; value not used directly in logic for now.
    _ = current_user

    raw = await audio.read()
    if not raw:
        raise APIError(
            status_code=422,
            code="stt_audio_empty",
            message="invalid request: audio payload is empty",
        )
    if len(raw) > settings.STT_MAX_FILE_SIZE_BYTES:
        raise APIError(
            status_code=413,
            code="stt_audio_too_large",
            message=(
                "payload too large: audio exceeds size limit; "
                f"limit_bytes={settings.STT_MAX_FILE_SIZE_BYTES}"
            ),
        )
    if sample_rate not in (8000, 16000):
        raise APIError(
            status_code=422,
            code="validation_failed",
            message=(
                "validation failed: request parameters are invalid; "
                f"field=sample_rate; reason=unsupported value {sample_rate}"
            ),
        )

    data = await service.transcribe_audio(
        audio_bytes=raw,
        audio_format=audio_format,
        sample_rate=sample_rate,
    )
    return success_response(data, status_code=200)


@router.get("/tts/messages/{assistant_candidate_id}/audio")
async def stream_tts_for_message(
    assistant_candidate_id: uuid.UUID,
    audio_format: Literal["opus", "mp3"] = Query("mp3"),
    current_user: User = Depends(deps.get_current_user),
    service: VoiceService = Depends(get_voice_service),
) -> StreamingResponse:
    mime_type, stream = await service.get_assistant_candidate_audio_stream(
        assistant_candidate_id=assistant_candidate_id,
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
