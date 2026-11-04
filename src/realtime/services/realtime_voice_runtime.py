"""Realtime speech runtime built on existing DashScope voice gateway."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import uuid

import av
import numpy as np

from src.core.config import settings
from src.core.tts_voice_catalog import legacy_dashscope_voice_id_for_minimax
from src.db.session import AsyncSessionLocal
from src.realtime.audio.debug_capture import RealtimeAudioDebugCapture
from src.realtime.pipeline.audio_pacer import AudioPacer
from src.realtime.services.sentence_segmenter import (
    SentenceSegmenter,
    SentenceSegmenterConfig,
)
from src.repositories.character_repository import CharacterRepository
from src.repositories.voice_gateway_repository import VoiceGatewayRepository
from src.services.tts_gateway_service import TTSGatewayService

_PCM_RATE_PATTERN = re.compile(r"(?:^|[;\s])rate=(\d+)(?:$|[;\s])", re.IGNORECASE)
logger = logging.getLogger(__name__)


def _pcm_sample_rate_from_mime_type(mime_type: str, *, default: int = 24000) -> int:
    if match := _PCM_RATE_PATTERN.search(mime_type):
        try:
            return int(match.group(1))
        except ValueError:
            return default
    return default


class _PCM16MonoResampler:
    def __init__(self, *, source_rate: int, target_rate: int) -> None:
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._resampler = (
            None
            if source_rate == target_rate
            else av.AudioResampler(format="s16", layout="mono", rate=target_rate)
        )

    def resample(self, samples: np.ndarray) -> np.ndarray:
        if samples.size == 0:
            return samples.astype(np.int16, copy=False)
        if self._resampler is None:
            return samples.astype(np.int16, copy=False)

        frame = av.AudioFrame(format="s16", layout="mono", samples=samples.size)
        frame.sample_rate = self.source_rate
        frame.planes[0].update(samples.astype(np.int16, copy=False).tobytes())
        resampled_frames = self._resampler.resample(frame)
        if not isinstance(resampled_frames, list):
            resampled_frames = [resampled_frames]

        chunks: list[np.ndarray] = []
        for resampled in resampled_frames:
            if resampled is None:
                continue
            chunks.append(resampled.to_ndarray().reshape(-1).astype(np.int16, copy=False))
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(chunks).astype(np.int16, copy=False)


class RealtimeVoiceRuntime:
    def __init__(
        self,
        voice_gateway_repo: VoiceGatewayRepository,
        *,
        tts_gateway_service: TTSGatewayService | None = None,
    ) -> None:
        self._voice_gateway_repo = voice_gateway_repo
        self._tts_gateway_service = tts_gateway_service or TTSGatewayService(
            dashscope_gateway=voice_gateway_repo
        )

    async def transcribe_pcm16_mono(self, audio_bytes: bytes, *, sample_rate: int = 16000) -> str:
        text, _ = await self._voice_gateway_repo.transcribe_audio(
            audio_bytes=audio_bytes,
            audio_format="pcm",
            sample_rate=sample_rate,
        )
        return text

    def open_streaming_stt(self):
        """Open a progressive ASR session, or None when unsupported."""
        return self._voice_gateway_repo.open_streaming_stt_session()

    async def stream_tts_to_pacer(
        self,
        *,
        segments_queue: asyncio.Queue[str | None],
        audio_pacer: AudioPacer,
        character_id: uuid.UUID,
        user_id: uuid.UUID,
        debug_capture: RealtimeAudioDebugCapture | None = None,
        generation_id: int | None = None,
    ) -> None:
        started_at = time.perf_counter()
        last_delta_at: float | None = None
        max_delta_gap_ms = 0
        delta_count = 0
        source_sample_count = 0
        target_sample_count = 0
        outcome = "completed"
        async with AsyncSessionLocal() as db:
            db.info["current_user_id"] = str(user_id)
            character_repo = CharacterRepository(db)
            character = await character_repo.get_by_id(character_id)
            provider = "dashscope"
            provider_model = settings.DASHSCOPE_TTS_MODEL
            provider_voice_id: str | None = None
            voice_source_type = "system"
            if character and character.voice_source_type == "clone":
                provider = character.voice_provider
                provider_model = character.voice_model
                provider_voice_id = character.voice_provider_voice_id
                voice_source_type = character.voice_source_type
            elif character:
                provider_voice_id = character.voice_provider_voice_id
                if character.voice_provider == "minimax":
                    provider_voice_id = legacy_dashscope_voice_id_for_minimax(
                        character.voice_provider_voice_id
                    )

        resampler: _PCM16MonoResampler | None = None
        segment_counter = [0]
        try:
            async for (
                audio_b64,
                mime_type,
            ) in self._tts_gateway_service.stream_tts_audio_base64_from_segments(
                segments_queue=segments_queue,
                audio_format="pcm",
                instructions=settings.DASHSCOPE_TTS_INSTRUCTIONS_DEFAULT or None,
                provider=provider,
                provider_voice_id=provider_voice_id,
                provider_model=provider_model,
                voice_source_type=voice_source_type,
                debug_capture=debug_capture,
                segment_counter=segment_counter,
            ):
                received_at = time.perf_counter()
                if last_delta_at is not None:
                    max_delta_gap_ms = max(
                        max_delta_gap_ms,
                        int((received_at - last_delta_at) * 1000),
                    )
                last_delta_at = received_at

                if not mime_type.startswith("audio/pcm"):
                    continue
                source_rate = _pcm_sample_rate_from_mime_type(mime_type)
                if (
                    resampler is None
                    or resampler.source_rate != source_rate
                    or resampler.target_rate != audio_pacer.sample_rate
                ):
                    resampler = _PCM16MonoResampler(
                        source_rate=source_rate,
                        target_rate=audio_pacer.sample_rate,
                    )
                pcm_source = np.frombuffer(base64.b64decode(audio_b64), dtype=np.int16)
                pcm_target = resampler.resample(pcm_source)
                delta_count += 1
                source_sample_count += int(pcm_source.size)
                target_sample_count += int(pcm_target.size)
                accepted = await audio_pacer.write_pcm16(
                    pcm_target.tobytes(),
                    generation_id=generation_id,
                )
                if not accepted:
                    raise asyncio.CancelledError
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            stats = await audio_pacer.stats_snapshot()
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            target_audio_ms = int(target_sample_count / audio_pacer.sample_rate * 1000)
            sent_segments = segment_counter[0]
            logger.info(
                "Realtime TTS audio stream %s: character_id=%s sent_segments=%s "
                "delta_count=%s source_samples=%s target_samples=%s "
                "target_audio_ms=%s max_delta_gap_ms=%s pacer_buffered_ms=%s "
                "pacer_underrun_count=%s pacer_partial_pad_count=%s",
                outcome,
                character_id,
                sent_segments,
                delta_count,
                source_sample_count,
                target_sample_count,
                target_audio_ms,
                max_delta_gap_ms,
                stats.buffered_ms,
                stats.underrun_count,
                stats.partial_pad_count,
                extra={
                    "event": "realtime.tts.audio_stream_summary",
                    "character_id": str(character_id),
                    "provider": provider,
                    "model": f"{provider}/{provider_model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": "pcm",
                    "elapsed_ms": elapsed_ms,
                },
            )
            if debug_capture is not None:
                debug_capture.record_event(
                    "tts.stream_summary",
                    {
                        "outcome": outcome,
                        "sent_segments": sent_segments,
                        "delta_count": delta_count,
                        "source_sample_count": source_sample_count,
                        "target_sample_count": target_sample_count,
                        "target_audio_ms": target_audio_ms,
                        "max_delta_gap_ms": max_delta_gap_ms,
                        "pacer_buffered_ms": stats.buffered_ms,
                        "pacer_underrun_count": stats.underrun_count,
                        "pacer_partial_pad_count": stats.partial_pad_count,
                    },
                )

    def build_sentence_segmenter(self) -> SentenceSegmenter:
        return SentenceSegmenter(
            SentenceSegmenterConfig(
                language="auto",
                min_length=10,
                max_length=max(80, settings.REALTIME_TTS_LATER_SEGMENT_MAX_CHARS),
                enable_smart_punctuation=True,
                latency_flush_min_length=14,
                latency_flush_target_length=settings.REALTIME_TTS_FIRST_SEGMENT_TARGET_CHARS,
                first_segment_timeout_ms=settings.REALTIME_TTS_FIRST_SEGMENT_TIMEOUT_MS,
                later_segment_timeout_ms=settings.REALTIME_TTS_LATER_SEGMENT_TIMEOUT_MS,
                later_segment_min_length=settings.REALTIME_TTS_LATER_SEGMENT_MIN_CHARS,
                later_segment_max_length=settings.REALTIME_TTS_LATER_SEGMENT_MAX_CHARS,
            )
        )
