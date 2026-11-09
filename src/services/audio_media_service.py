"""Chat audio storage and cropping for the character forge voice-clone flow."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
import numpy as np

from src.core.config import settings
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.schemas.media import ChatAudioUploadResponse
from src.services.local_media_store import LocalMediaStore
from src.services.media_sync_worker import MediaSyncWorker
from src.services.media_url_service import (
    chat_audio_key,
    original_object_key,
)
from src.services.r2_storage_service import R2StorageService

logger = logging.getLogger(__name__)

_FORMAT_BY_SUFFIX = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".m4a": "m4a",
    ".mp4": "m4a",
    ".flac": "flac",
    ".opus": "opus",
}
_FORMAT_BY_MIME = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/opus": "opus",
}
_MIME_BY_FORMAT = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "opus": "audio/opus",
}
_CROP_SAMPLE_RATE = 24_000
_PRIVATE_CACHE_CONTROL = "private, max-age=0"

# Analysis parameters mirror what the manual probing used: 50 ms RMS frames and
# pauses that sit clearly below the recording's median level.
_ANALYSIS_SAMPLE_RATE = 16_000
_RMS_WINDOW_SECONDS = 0.05
_QUIET_DROP_DB = 6.0
_MIN_GAP_SECONDS = 0.15
_MAX_GAP_SECONDS = 0.7
_MIN_CLIP_SECONDS = 3.0


@dataclass(frozen=True)
class CroppedAudio:
    body: bytes
    content_type: str
    format: str
    duration_seconds: float
    cropped: bool


@dataclass(frozen=True)
class AudioInfo:
    duration_seconds: float
    sample_rate: int
    channels: int
    codec: str
    format: str
    size_bytes: int


@dataclass(frozen=True)
class AudioAnalysis:
    """Enough about a recording for the agent to decide how to cut it."""

    duration_seconds: float
    rms_db_median: float
    quiet_ratio: float
    longest_quiet_gap_seconds: float
    suggested_start_seconds: float
    suggested_duration_seconds: float


class AudioMediaService:
    """Stores uploaded recordings and produces the prompt clip used for cloning."""

    def __init__(
        self,
        r2_storage: R2StorageService,
        local_store: LocalMediaStore,
        sync_worker: MediaSyncWorker,
    ):
        self._r2_storage = r2_storage
        self._local_store = local_store
        self._sync_worker = sync_worker

    @staticmethod
    def resolve_format(*, content_type: str | None, filename: str | None) -> str | None:
        if content_type:
            normalized = content_type.split(";", 1)[0].strip().lower()
            fmt = _FORMAT_BY_MIME.get(normalized)
            if fmt:
                return fmt
        if filename:
            fmt = _FORMAT_BY_SUFFIX.get(Path(filename).suffix.lower())
            if fmt:
                return fmt
        return None

    async def store_chat_audio(
        self,
        *,
        owner_id: uuid.UUID | str,
        body: bytes,
        filename: str | None,
        content_type: str | None,
    ) -> ChatAudioUploadResponse:
        if not body:
            raise APIError(
                status_code=422,
                code=ErrorCode.VALIDATION_FAILED,
                message="invalid upload: audio file is empty",
            )
        if len(body) > settings.CHAT_AUDIO_MAX_UPLOAD_BYTES:
            limit_mb = max(1, settings.CHAT_AUDIO_MAX_UPLOAD_BYTES // 1024 // 1024)
            raise APIError(
                status_code=413,
                code=ErrorCode.UPLOAD_FILE_TOO_LARGE,
                message=f"invalid upload: audio file exceeds limit; limit_mb={limit_mb}",
            )

        fmt = self.resolve_format(content_type=content_type, filename=filename)
        allowed = settings.chat_audio_allowed_formats
        if fmt is None or fmt not in allowed:
            raise APIError(
                status_code=415,
                code=ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
                message=f"invalid upload: audio format not allowed; allowed={','.join(allowed)}",
            )

        duration = await self.probe_duration_seconds(body, fmt)
        if duration <= 0:
            raise APIError(
                status_code=422,
                code=ErrorCode.VALIDATION_FAILED,
                message="invalid upload: audio duration could not be detected",
            )

        audio_key = chat_audio_key(str(owner_id), f"{uuid.uuid4().hex}.{fmt}")
        mime_type = _MIME_BY_FORMAT[fmt]
        object_key = original_object_key(audio_key)
        staged = await anyio.to_thread.run_sync(
            lambda: self._local_store.put(
                object_key=object_key,
                body=body,
                content_type=mime_type,
                cache_control=_PRIVATE_CACHE_CONTROL,
            )
        )
        if not staged:
            raise APIError(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="internal error: failed to stage uploaded recording",
            )
        self._sync_worker.enqueue(object_key)
        logger.info(
            "chat audio stored: audio_key=%s bytes=%d duration=%.2fs format=%s",
            audio_key,
            len(body),
            duration,
            fmt,
        )
        return ChatAudioUploadResponse(
            audio_key=audio_key,
            duration_seconds=round(duration, 2),
            size_bytes=len(body),
            mime_type=mime_type,
            name=(filename or "").strip()[:120] or f"录音.{fmt}",
        )

    @staticmethod
    def format_from_key(audio_key: str) -> str:
        fmt = _FORMAT_BY_SUFFIX.get(Path(audio_key).suffix.lower())
        if fmt is None:
            raise APIError(
                status_code=422,
                code=ErrorCode.VALIDATION_FAILED,
                message="invalid request: audio_key has no recognised audio suffix",
            )
        return fmt

    async def load_chat_audio(self, audio_key: str) -> bytes:
        object_key = original_object_key(audio_key)
        staged = await anyio.to_thread.run_sync(self._local_store.get, object_key)
        if staged is not None:
            return staged.body
        stored = await self._r2_storage.get_object(object_key=object_key)
        return stored.body

    async def probe_duration_seconds(self, body: bytes, fmt: str) -> float:
        with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as handle:
            handle.write(body)
            path = Path(handle.name)
        try:
            code, stdout, stderr = await self._run(
                [
                    settings.FFPROBE_BINARY,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ]
            )
            if code != 0:
                logger.warning("ffprobe failed: %s", stderr.decode("utf-8", "ignore").strip())
                raise APIError(
                    status_code=422,
                    code=ErrorCode.VALIDATION_FAILED,
                    message="invalid upload: audio file could not be decoded",
                )
            raw = stdout.decode("utf-8", "ignore").strip()
            return float(raw) if raw else 0.0
        finally:
            path.unlink(missing_ok=True)

    async def probe_info(self, body: bytes, fmt: str) -> AudioInfo:
        with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as handle:
            handle.write(body)
            path = Path(handle.name)
        try:
            code, stdout, stderr = await self._run(
                [
                    settings.FFPROBE_BINARY,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name,sample_rate,channels:format=duration",
                    "-of",
                    "json",
                    str(path),
                ]
            )
            if code != 0:
                logger.warning("ffprobe failed: %s", stderr.decode("utf-8", "ignore").strip())
                raise APIError(
                    status_code=422,
                    code=ErrorCode.VALIDATION_FAILED,
                    message="invalid upload: audio file could not be decoded",
                )
            payload = json.loads(stdout.decode("utf-8", "ignore") or "{}")
            stream = (payload.get("streams") or [{}])[0]
            return AudioInfo(
                duration_seconds=float((payload.get("format") or {}).get("duration") or 0.0),
                sample_rate=int(stream.get("sample_rate") or 0),
                channels=int(stream.get("channels") or 0),
                codec=str(stream.get("codec_name") or ""),
                format=fmt,
                size_bytes=len(body),
            )
        finally:
            path.unlink(missing_ok=True)

    async def analyze(
        self,
        body: bytes,
        fmt: str,
        *,
        target_seconds: float,
    ) -> AudioAnalysis:
        """Locate a long breath-free stretch so the agent can cut a clean prompt clip."""

        info = await self.probe_info(body, fmt)
        duration = info.duration_seconds
        samples = await self._decode_pcm(body, fmt, sample_rate=_ANALYSIS_SAMPLE_RATE)

        window = int(_ANALYSIS_SAMPLE_RATE * _RMS_WINDOW_SECONDS)
        frame_count = len(samples) // window if window else 0
        if frame_count <= 0:
            return AudioAnalysis(
                duration_seconds=duration,
                rms_db_median=-120.0,
                quiet_ratio=0.0,
                longest_quiet_gap_seconds=0.0,
                suggested_start_seconds=max(0.0, (duration - target_seconds) / 2),
                suggested_duration_seconds=min(target_seconds, duration),
            )

        frames = samples[: frame_count * window].reshape(frame_count, window)
        rms = np.sqrt((frames**2).mean(axis=1)) + 1e-9
        db = 20 * np.log10(rms)
        median_db = float(np.median(db))

        quiet = db < (median_db - _QUIET_DROP_DB)
        quiet_ratio = float(quiet.mean())
        # Pauses long enough to be a breath, not just a consonant closure.
        runs: list[tuple[float, float]] = []
        start: int | None = None
        for index, is_quiet in enumerate(quiet):
            if is_quiet and start is None:
                start = index
            elif not is_quiet and start is not None:
                begin = start * _RMS_WINDOW_SECONDS
                end = index * _RMS_WINDOW_SECONDS
                if _MIN_GAP_SECONDS <= end - begin <= _MAX_GAP_SECONDS:
                    runs.append((begin, end))
                start = None

        longest_gap = max((end - begin for begin, end in runs), default=0.0)
        speech_start: float | None = None
        if len(runs) >= 2:
            best = 0.0
            for (_, first_end), (second_start, _) in zip(runs, runs[1:]):
                span = second_start - first_end
                if span > best:
                    best = span
                    speech_start = first_end
        if speech_start is None:
            speech_start = max(0.0, (duration - target_seconds) / 2)

        clip = min(target_seconds, duration)
        latest_start = max(0.0, duration - clip)
        return AudioAnalysis(
            duration_seconds=duration,
            rms_db_median=median_db,
            quiet_ratio=quiet_ratio,
            longest_quiet_gap_seconds=longest_gap,
            suggested_start_seconds=min(max(0.0, speech_start), latest_start),
            suggested_duration_seconds=clip,
        )

    async def crop(
        self,
        body: bytes,
        fmt: str,
        *,
        start_seconds: float | None = None,
        duration_seconds: float | None = None,
    ) -> CroppedAudio:
        """Cut an arbitrary window and re-encode it to mono WAV for cloning."""

        info = await self.probe_info(body, fmt)
        total = info.duration_seconds
        clip = min(duration_seconds, total) if duration_seconds else total
        clip = max(clip, _MIN_CLIP_SECONDS)
        latest_start = max(0.0, total - clip)
        start = 0.0 if start_seconds is None else min(max(0.0, start_seconds), latest_start)

        unchanged = start <= 0.0 and clip >= total
        if unchanged:
            return CroppedAudio(
                body=body,
                content_type=_MIME_BY_FORMAT[fmt],
                format=fmt,
                duration_seconds=total,
                cropped=False,
            )

        with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as handle:
            handle.write(body)
            source = Path(handle.name)
        target = source.with_suffix(".cropped.wav")
        try:
            code, _, stderr = await self._run(
                [
                    settings.FFMPEG_BINARY,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-t",
                    f"{clip:.3f}",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    str(_CROP_SAMPLE_RATE),
                    "-c:a",
                    "pcm_s16le",
                    str(target),
                ]
            )
            if code != 0 or not target.exists():
                logger.warning("ffmpeg crop failed: %s", stderr.decode("utf-8", "ignore").strip())
                raise APIError(
                    status_code=500,
                    code=ErrorCode.INTERNAL_ERROR,
                    message="internal error: audio crop failed",
                )
            return CroppedAudio(
                body=target.read_bytes(),
                content_type="audio/wav",
                format="wav",
                duration_seconds=clip,
                cropped=True,
            )
        finally:
            source.unlink(missing_ok=True)
            target.unlink(missing_ok=True)

    async def _decode_pcm(self, body: bytes, fmt: str, *, sample_rate: int):
        with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as handle:
            handle.write(body)
            source = Path(handle.name)
        try:
            code, stdout, stderr = await self._run(
                [
                    settings.FFMPEG_BINARY,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    str(sample_rate),
                    "-f",
                    "f32le",
                    "pipe:1",
                ]
            )
            if code != 0:
                logger.warning("ffmpeg decode failed: %s", stderr.decode("utf-8", "ignore").strip())
                raise APIError(
                    status_code=422,
                    code=ErrorCode.VALIDATION_FAILED,
                    message="invalid upload: audio file could not be decoded",
                )
            return np.frombuffer(stdout, dtype=np.float32)
        finally:
            source.unlink(missing_ok=True)

    @staticmethod
    async def _run(args: list[str]) -> tuple[int, bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise APIError(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message=f"internal error: media tool not installed; binary={args[0]}",
            ) from exc
        stdout, stderr = await process.communicate()
        return process.returncode or 0, stdout or b"", stderr or b""
