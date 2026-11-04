"""Service layer for Phase 2 voice features."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from src.core.config import settings
from src.core.exceptions import APIError
from src.core.tts_registry import (
    DEFAULT_SYSTEM_TTS_MODEL,
    DEFAULT_SYSTEM_TTS_PROVIDER,
    DEFAULT_SYSTEM_TTS_VOICE_ID,
)
from src.db.models import User, VoiceSourceType
from src.repositories.candidate_repository import CandidateRepository
from src.repositories.character_repository import CharacterRepository
from src.repositories.voice_gateway_repository import VoiceGatewayRepository
from src.schemas.voice import STTTranscriptionResult
from src.services.tts_gateway_service import TTSGatewayService

logger = logging.getLogger(__name__)


def _decode_audio_b64(audio_b64: str) -> bytes:
    payload = audio_b64.strip()
    if not payload:
        return b""
    missing_padding = len(payload) % 4
    if missing_padding:
        payload = payload + ("=" * (4 - missing_padding))
    return base64.b64decode(payload)


async def _next_decoded_audio_chunk(
    stream: AsyncGenerator[tuple[str, str], None],
) -> tuple[bytes, str] | None:
    async for audio_b64, mime_type in stream:
        decoded = _decode_audio_b64(audio_b64)
        if decoded:
            return decoded, mime_type
    return None


class VoiceService:
    """Business logic for speech transcription and synthesis."""

    def __init__(
        self,
        voice_gateway_repo: VoiceGatewayRepository,
        candidate_repo: CandidateRepository,
        character_repo: CharacterRepository | None = None,
        *,
        tts_gateway_service: TTSGatewayService | None = None,
    ) -> None:
        self._voice_gateway_repo = voice_gateway_repo
        self._tts_gateway_service = tts_gateway_service or TTSGatewayService(
            dashscope_gateway=voice_gateway_repo
        )
        self._candidate_repo = candidate_repo
        self._character_repo = character_repo

    async def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        audio_format: str,
        sample_rate: int,
    ) -> STTTranscriptionResult:
        text, request_id = await self._voice_gateway_repo.transcribe_audio(
            audio_bytes=audio_bytes,
            audio_format=audio_format,
            sample_rate=sample_rate,
        )
        return STTTranscriptionResult(
            text=text,
            model=settings.DASHSCOPE_STT_MODEL,
            request_id=request_id,
        )

    async def get_assistant_candidate_audio_stream(
        self,
        *,
        assistant_candidate_id: uuid.UUID,
        current_user: User,
        audio_format: str,
    ) -> tuple[str, AsyncGenerator[bytes, None]]:
        row = await self._candidate_repo.get_assistant_candidate_with_chat(
            candidate_id=assistant_candidate_id,
            user_id=current_user.id,
        )
        if not row:
            any_row = await self._candidate_repo.get_candidate_with_turn_and_chat(
                candidate_id=assistant_candidate_id
            )
            if not any_row:
                raise APIError(
                    status_code=404,
                    code="candidate_not_found",
                    message=(
                        "resource not found: candidate does not exist; "
                        f"candidate_id={assistant_candidate_id}"
                    ),
                )
            _, turn, chat = any_row
            if chat.user_id != current_user.id:
                raise APIError(
                    status_code=403,
                    code="tts_candidate_forbidden",
                    message=(
                        "authorization failed: candidate is not accessible; "
                        f"candidate_id={assistant_candidate_id}"
                    ),
                )
            raise APIError(
                status_code=409,
                code="tts_candidate_invalid_author",
                message=(
                    "resource conflict: candidate is not assistant-authored; "
                    f"candidate_id={assistant_candidate_id}; turn_id={turn.id}"
                ),
            )

        candidate, _, _ = row
        provider = DEFAULT_SYSTEM_TTS_PROVIDER
        provider_voice_id: str | None = DEFAULT_SYSTEM_TTS_VOICE_ID
        provider_model: str | None = DEFAULT_SYSTEM_TTS_MODEL
        voice_source_type = VoiceSourceType.SYSTEM.value
        if self._character_repo:
            _, _, chat = row
            character = await self._character_repo.get_by_id(chat.character_id)
            if character:
                provider = character.voice_provider
                provider_voice_id = character.voice_provider_voice_id
                provider_model = character.voice_model
                voice_source_type = character.voice_source_type
        source_stream = self._tts_gateway_service.stream_tts_audio_base64(
            text=candidate.content,
            audio_format=audio_format,
            instructions=settings.DASHSCOPE_TTS_INSTRUCTIONS_DEFAULT or None,
            provider=provider,
            provider_voice_id=provider_voice_id,
            provider_model=provider_model,
            voice_source_type=voice_source_type,
        )
        first_chunk = await _next_decoded_audio_chunk(source_stream)
        if first_chunk is None:
            raise APIError(
                status_code=502,
                code="tts_upstream_error",
                message="upstream error: tts service produced empty audio stream",
            )

        first_chunk_bytes, mime_type = first_chunk

        async def _audio_generator() -> AsyncGenerator[bytes, None]:
            yield first_chunk_bytes
            try:
                while True:
                    next_chunk = await _next_decoded_audio_chunk(source_stream)
                    if next_chunk is None:
                        break
                    chunk, _ = next_chunk
                    yield chunk
            except APIError as exc:
                # HTTP response has started, so terminate stream quietly.
                logger.warning("TTS stream interrupted after first chunk: %s", exc.message)
            except Exception as exc:
                logger.exception("Unexpected TTS stream interruption: %s", exc)

        return mime_type, _audio_generator()

    async def stream_tts_sse_events(
        self,
        *,
        text: str,
        assistant_candidate_id: str,
        audio_format: str = "pcm",
        instructions: str | None = None,
        emit_done: bool = True,
        provider: str = DEFAULT_SYSTEM_TTS_PROVIDER,
        provider_voice_id: str | None = None,
        provider_model: str | None = None,
        voice_source_type: str = VoiceSourceType.SYSTEM.value,
    ) -> AsyncGenerator[dict[str, Any], None]:
        seq = 0
        async for audio_b64, mime_type in self._tts_gateway_service.stream_tts_audio_base64(
            text=text,
            audio_format=audio_format,
            instructions=instructions or settings.DASHSCOPE_TTS_INSTRUCTIONS_DEFAULT or None,
            provider=provider,
            provider_voice_id=provider_voice_id,
            provider_model=provider_model,
            voice_source_type=voice_source_type,
        ):
            seq += 1
            yield {
                "type": "tts_audio_delta",
                "assistant_candidate_id": assistant_candidate_id,
                "seq": seq,
                "audio_b64": audio_b64,
                "mime_type": mime_type,
            }
        if emit_done:
            yield {
                "type": "tts_audio_done",
                "assistant_candidate_id": assistant_candidate_id,
            }

    async def stream_tts_sse_events_from_segments(
        self,
        *,
        segments_queue: asyncio.Queue[str | None],
        assistant_candidate_id: str,
        audio_format: str = "pcm",
        instructions: str | None = None,
        emit_done: bool = True,
        provider: str = DEFAULT_SYSTEM_TTS_PROVIDER,
        provider_voice_id: str | None = None,
        provider_model: str | None = None,
        voice_source_type: str = VoiceSourceType.SYSTEM.value,
    ) -> AsyncGenerator[dict[str, Any], None]:
        seq = 0
        async for (
            audio_b64,
            mime_type,
        ) in self._tts_gateway_service.stream_tts_audio_base64_from_segments(
            segments_queue=segments_queue,
            audio_format=audio_format,
            instructions=instructions or settings.DASHSCOPE_TTS_INSTRUCTIONS_DEFAULT or None,
            provider=provider,
            provider_voice_id=provider_voice_id,
            provider_model=provider_model,
            voice_source_type=voice_source_type,
        ):
            seq += 1
            yield {
                "type": "tts_audio_delta",
                "assistant_candidate_id": assistant_candidate_id,
                "seq": seq,
                "audio_b64": audio_b64,
                "mime_type": mime_type,
            }
        if emit_done:
            yield {
                "type": "tts_audio_done",
                "assistant_candidate_id": assistant_candidate_id,
            }

    async def warm_tts_sse_events_from_segments(
        self,
        *,
        audio_format: str = "pcm",
        provider: str = DEFAULT_SYSTEM_TTS_PROVIDER,
        provider_voice_id: str | None = None,
        provider_model: str | None = None,
        voice_source_type: str = VoiceSourceType.SYSTEM.value,
    ) -> bool:
        return await self._tts_gateway_service.warm_tts_audio_base64_from_segments(
            audio_format=audio_format,
            provider=provider,
            provider_voice_id=provider_voice_id,
            provider_model=provider_model,
            voice_source_type=voice_source_type,
        )
