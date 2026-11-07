"""Gateway repository for DashScope realtime STT/TTS."""

from __future__ import annotations

import asyncio
import base64
import errno
import io
import json
import logging
import re
import time
import uuid
import wave
from collections.abc import AsyncGenerator, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from src.core.config import settings
from src.core.exceptions import APIError
from src.observability import get_client, update_observation_safe

logger = logging.getLogger(__name__)

_DASHSCOPE_PROVIDER = "dashscope"
_LANGFUSE_UNIT_CHARACTERS = "CHARACTERS"
_LANGFUSE_UNIT_SECONDS = "SECONDS"
_LANGFUSE_UNIT_REQUESTS = "REQUESTS"
_LANGFUSE_UNIT_TOKENS = "TOKENS"
_STT_WEBSOCKET_MAX_ATTEMPTS = 2
_STT_WEBSOCKET_RETRY_DELAY_SECONDS = 0.2
_TRANSIENT_STT_WEBSOCKET_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
    10051,  # WSAENETUNREACH
    10053,  # WSAECONNABORTED
    10054,  # WSAECONNRESET
    10060,  # WSAETIMEDOUT
    10061,  # WSAECONNREFUSED
    10065,  # WSAEHOSTUNREACH
}

# Hardcoded voice catalog for qwen3-tts-instruct-flash-realtime (24 voices).
# Source: DashScope API — not dependent on the Aliyun documentation page.
_QWEN3_TTS_VOICE_CATALOG: list[dict[str, str | None]] = [
    {"provider_voice_id": "Cherry", "display_name": "Cherry", "usage_hint": "Warm, sunny, natural young female", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/df6b8b7237b24396aebadd08a627e938"},
    {"provider_voice_id": "Serena", "display_name": "Serena", "usage_hint": "Gentle, soft-spoken female", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/483fb9cf745e41e9bb1c4f63fc0493a6"},
    {"provider_voice_id": "Ethan", "display_name": "Ethan", "usage_hint": "Energetic, bright young male", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/e95590f5a4314e098307c2407b078497"},
    {"provider_voice_id": "Chelsie", "display_name": "Chelsie", "usage_hint": "Anime-style virtual girlfriend", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/856f560754f94f58b2f7af051fea68d7"},
    {"provider_voice_id": "Momo", "display_name": "Momo", "usage_hint": "Playful, mischievous, cutesy girl", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/a43f593a41c4470e95808b3e043c31a2"},
    {"provider_voice_id": "Vivian", "display_name": "Vivian", "usage_hint": "Sassy, feisty girl with charm", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/c6e424255a0b47afbdae7245deb5e5fb"},
    {"provider_voice_id": "Moon", "display_name": "Moon", "usage_hint": "Cool, confident male", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/f8f6d860eb1947548b5fe930d67e79f5"},
    {"provider_voice_id": "Maia", "display_name": "Maia", "usage_hint": "Intellectual and gentle", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/6842d945a890472b9a2d2883125dc9ae"},
    {"provider_voice_id": "Kai", "display_name": "Kai", "usage_hint": "Relaxed, soothing male", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/c1ca0129ea00430caf272882176e1b20"},
    {"provider_voice_id": "Nofish", "display_name": "Nofish", "usage_hint": "Laid-back designer with a light accent", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/36f626b3d6c344bf9d6cfe37c4253458"},
    {"provider_voice_id": "Bella", "display_name": "Bella", "usage_hint": "Adorable little girl", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/68687adc781b4bc7ba5c9533f5a83159"},
    {"provider_voice_id": "Eldric Sage", "display_name": "Eldric Sage", "usage_hint": "Calm, wise older male", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/2e185ff240594e9ca41359fe4d630505"},
    {"provider_voice_id": "Mia", "display_name": "Mia", "usage_hint": "Sweet, obedient girl", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/3867b24c2396439396a62a7ef6a41a07"},
    {"provider_voice_id": "Mochi", "display_name": "Mochi", "usage_hint": "Bright, precocious little boy", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/310741c03faa496784b2db725a9d5435"},
    {"provider_voice_id": "Bellona", "display_name": "Bellona", "usage_hint": "Bold, clear-voiced heroine", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/3877e6e569cf4ee6a7b9a0ac207644f0"},
    {"provider_voice_id": "Vincent", "display_name": "Vincent", "usage_hint": "Raspy, smoky-voiced older male", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/ee98fd0dde394f13ba57d18c2bb6d028"},
    {"provider_voice_id": "Bunny", "display_name": "Bunny", "usage_hint": "Cute anime-style girl", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/e9ecb32af27b456fae323ec154337bd4"},
    {"provider_voice_id": "Neil", "display_name": "Neil", "usage_hint": "Crisp, articulate news-anchor male", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/abb934dd6a9947b689018c483c555d63"},
    {"provider_voice_id": "Elias", "display_name": "Elias", "usage_hint": "Precise, articulate female narrator", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/5d9393a243c14ea2a97f4ef1305b85f1"},
    {"provider_voice_id": "Arthur", "display_name": "Arthur", "usage_hint": "Plain-spoken, weathered storyteller", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/c32211d5306444449c28d5cf782c9738"},
    {"provider_voice_id": "Nini", "display_name": "Nini", "usage_hint": "Sweet, clingy, honey-voiced girl", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/be61a021b6fe48829b1f0cab3221505b"},
    {"provider_voice_id": "Seren", "display_name": "Seren", "usage_hint": "Calm, soothing, sleep-friendly female", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/355047e814674d86be1ad617e8d95c0e"},
    {"provider_voice_id": "Pip", "display_name": "Pip", "usage_hint": "Playful, childlike little boy", "language": "English", "gender": "Male", "avatar_image_key": "images/avatars/voices/system/c8adf30c359f47f8a7b486d220c04f9b"},
    {"provider_voice_id": "Stella", "display_name": "Stella", "usage_hint": "Sweet, dreamy girl with a heroic streak", "language": "English", "gender": "Female", "avatar_image_key": "images/avatars/voices/system/30dd1b07fdd647f8b5de022bcbc02de7"},
]

_QWEN3_TTS_VOICE_MAP: dict[str, list[dict[str, str | None]]] = {
    "qwen3_tts": _QWEN3_TTS_VOICE_CATALOG,
}


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _load_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_event_type(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    return event_type if isinstance(event_type, str) else None


def _extract_event_message(event: dict[str, Any], default: str) -> str:
    message = event.get("message")
    if isinstance(message, str) and message.strip():
        return message
    error = event.get("error")
    if isinstance(error, dict):
        nested_message = error.get("message")
        if isinstance(nested_message, str) and nested_message.strip():
            return nested_message
    return default


def _extract_stt_error_message(event: dict[str, Any], default: str) -> str:
    """Extract STT websocket error message from multiple possible locations."""
    message = event.get("message")
    if isinstance(message, str) and message.strip():
        return message

    header = event.get("header")
    if isinstance(header, dict):
        header_message = header.get("error_message")
        if isinstance(header_message, str) and header_message.strip():
            return header_message

    payload = event.get("payload")
    if isinstance(payload, dict):
        payload_message = payload.get("message")
        if isinstance(payload_message, str) and payload_message.strip():
            return payload_message

    return default


def _is_qwen3_realtime_asr_model(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith("qwen3-asr-flash-realtime")


def _parse_stt_language_hints(value: str) -> list[str]:
    hints = [item.strip().lower() for item in (value or "").split(",") if item.strip()]
    return hints[:4]


def _pcm16_to_wav(pcm16: bytes, *, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16)
    return buffer.getvalue()


def _build_stt_http_audio(
    *,
    audio_bytes: bytes,
    audio_format: str,
    sample_rate: int,
) -> tuple[str, str]:
    """Return the Base64 Data URI and the upstream ``format`` value."""
    normalized = audio_format.strip().lower()
    if normalized == "wav":
        payload = audio_bytes
    elif normalized == "pcm":
        payload = _pcm16_to_wav(audio_bytes, sample_rate=sample_rate)
    else:
        raise APIError(
            status_code=422,
            code="validation_failed",
            message=(
                "validation failed: unsupported audio format for http stt; "
                f"audio_format={audio_format}; supported=pcm|wav"
            ),
        )
    return "data:audio/wav;base64," + base64.b64encode(payload).decode("ascii"), "wav"


def _build_stt_http_messages(
    *,
    audio_data_uri: str,
    context_messages: list[dict[str, str]] | None,
) -> list[dict[str, Any]]:
    """Build ``input.messages`` with context first and the audio message last."""
    messages: list[dict[str, Any]] = []
    for item in context_messages or []:
        role = item.get("role")
        text = (item.get("text") or "").strip()
        if role not in {"user", "assistant"} or not text:
            continue
        content_type = "input_text" if role == "user" else "text"
        messages.append(
            {"role": role, "content": [{"type": content_type, "text": text[:400]}]}
        )
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": audio_data_uri}}
            ],
        }
    )
    return messages


def _extract_stt_http_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if not isinstance(output, dict):
        return ""
    text = output.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    sentence = output.get("sentence")
    if isinstance(sentence, dict):
        value = sentence.get("text")
        if isinstance(value, str):
            return value.strip()
    return ""


def _extract_stt_http_usage(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    return usage if isinstance(usage, dict) else {}


def _extract_qwen3_transcription_text(event: dict[str, Any]) -> str:
    for key in ("transcript", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str):
            return value.strip()

    item = event.get("item")
    if isinstance(item, dict):
        for key in ("transcript", "text", "delta"):
            value = item.get(key)
            if isinstance(value, str):
                return value.strip()

    response = event.get("response")
    if isinstance(response, dict):
        for key in ("transcript", "text"):
            value = response.get(key)
            if isinstance(value, str):
                return value.strip()

    return ""


def _append_or_replace_query_param(url: str, *, key: str, value: str) -> str:
    parsed = urlsplit(url)
    query_items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != key]
    query_items.append((key, value))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_items),
            parsed.fragment,
        )
    )


def _new_event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def _voice_profile_session_params(voice_profile: dict[str, Any] | None) -> dict[str, Any]:
    """Map a stored character voice profile onto Qwen3-TTS session fields."""
    if not voice_profile:
        return {}
    from src.core.voice_profiles import voice_profile_or_default

    profile = voice_profile_or_default(voice_profile)
    return profile.session_params()


def _extract_stt_text(payload: dict[str, Any]) -> str:
    sentence = payload.get("sentence")
    if isinstance(sentence, dict):
        text = sentence.get("text")
        if isinstance(text, str):
            return text.strip()

    output = payload.get("output")
    if isinstance(output, dict):
        for key in ("sentence", "result", "text", "transcript"):
            value = output.get(key)
            if isinstance(value, dict):
                nested = value.get("text")
                if isinstance(nested, str):
                    return nested.strip()
            if isinstance(value, str):
                return value.strip()

    for key in ("text", "transcript", "result"):
        value = payload.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _merge_incremental_text(parts: list[str], text: str) -> None:
    text = text.strip()
    if not text:
        return
    if not parts:
        parts.append(text)
        return
    last = parts[-1]
    if text == last:
        return
    if text.startswith(last):
        parts[-1] = text
        return
    if last.startswith(text):
        return
    parts.append(text)


def _dashscope_model_label(model: str) -> str:
    return f"{_DASHSCOPE_PROVIDER}/{model.strip()}"


def _infer_dashscope_billing_region(*urls: str) -> str:
    configured = (settings.DASHSCOPE_BILLING_REGION or "").strip().lower()
    if configured:
        return configured

    joined = " ".join(url for url in urls if url).lower()
    if "dashscope-intl.aliyuncs.com" in joined:
        return "international"
    if "dashscope-us.aliyuncs.com" in joined:
        return "us"
    return "cn"


def _is_transient_stt_websocket_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            ConnectionAbortedError,
            ConnectionRefusedError,
            ConnectionResetError,
            BrokenPipeError,
        ),
    ):
        return True
    if not isinstance(exc, OSError):
        return False

    for attr in ("errno", "winerror"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and code in _TRANSIENT_STT_WEBSOCKET_ERRNOS:
            return True
    return False


def _seconds_usage_details(seconds: float) -> dict[str, Any]:
    int_value = max(1, round(seconds)) if seconds > 0 else 0
    return {
        "input": int_value,
        "total": int_value,
    }


def _characters_usage_details(characters: int) -> dict[str, Any]:
    count = max(characters, 0)
    return {
        "input": count,
        "total": count,
        "unit": _LANGFUSE_UNIT_CHARACTERS,
    }


def _requests_usage_details(count: int = 1) -> dict[str, Any]:
    normalized = max(count, 0)
    return {
        "input": normalized,
        "total": normalized,
        "unit": _LANGFUSE_UNIT_REQUESTS,
    }


def _pcm16_mono_duration_seconds(audio_bytes: bytes, *, sample_rate: int) -> float:
    if sample_rate <= 0:
        return 0.0
    return len(audio_bytes) / float(sample_rate * 2)


def _wav_duration_seconds(audio_bytes: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
    except Exception:
        return None
    if frame_rate <= 0:
        return None
    return frame_count / float(frame_rate)


def _estimate_audio_duration_seconds(
    *,
    audio_bytes: bytes,
    audio_format: str,
    sample_rate: int,
    prepared_audio_bytes: bytes | None = None,
    prepared_audio_format: str | None = None,
) -> tuple[float | None, str]:
    normalized_format = audio_format.strip().lower()
    if normalized_format == "wav":
        duration = _wav_duration_seconds(audio_bytes)
        if duration is not None:
            return duration, "wav_header"

    prepared_format = (prepared_audio_format or "").strip().lower()
    if prepared_format == "pcm" and prepared_audio_bytes is not None:
        return (
            _pcm16_mono_duration_seconds(prepared_audio_bytes, sample_rate=sample_rate),
            "prepared_pcm16_mono_bytes",
        )

    if normalized_format == "pcm":
        return (
            _pcm16_mono_duration_seconds(audio_bytes, sample_rate=sample_rate),
            "pcm16_mono_bytes",
        )

    return None, "unavailable"


def _start_voice_model_observation(
    *,
    name: str,
    model: str,
    input_payload: Any,
    metadata: dict[str, Any],
) -> Any | None:
    starter = getattr(get_client(), "start_observation", None)
    if not callable(starter):
        return None
    return starter(
        name=name,
        as_type="generation",
        input=input_payload,
        model=_dashscope_model_label(model),
        metadata=metadata,
    )


def _end_voice_model_observation(
    observation: Any | None,
    *,
    output: Any | None = None,
    usage_details: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    level: str | None = None,
    status_message: str | None = None,
) -> None:
    payload: dict[str, Any] = {}
    if output is not None:
        payload["output"] = output
    if usage_details:
        payload["usage_details"] = usage_details
    if metadata:
        payload["metadata"] = metadata
    if level:
        payload["level"] = level
    if status_message:
        payload["status_message"] = status_message
    if payload:
        update_observation_safe(observation, **payload)
    if observation is not None and hasattr(observation, "end"):
        observation.end()


class VoiceGatewayRepository:
    """DashScope transport wrapper for speech services."""

    def __init__(self) -> None:
        self._api_key = settings.DASHSCOPE_API_KEY
        self._stt_model = settings.DASHSCOPE_STT_MODEL
        self._tts_model = settings.DASHSCOPE_TTS_MODEL
        self._tts_language_type = settings.DASHSCOPE_TTS_LANGUAGE_TYPE.strip() or "English"
        self._voice_enrollment_model = settings.DASHSCOPE_VOICE_ENROLLMENT_MODEL
        self._voice_clone_target_model = settings.DASHSCOPE_VOICE_CLONE_TARGET_MODEL
        self._tts_customization_api_url = settings.DASHSCOPE_TTS_CUSTOMIZATION_API_URL
        self._http_timeout_seconds = settings.DASHSCOPE_HTTP_TIMEOUT_SECONDS
        self._stt_event_timeout_seconds = settings.DASHSCOPE_STT_EVENT_TIMEOUT_SECONDS
        self._stt_http_model = settings.DASHSCOPE_STT_HTTP_MODEL
        self._stt_http_api_url = settings.DASHSCOPE_STT_HTTP_API_URL
        self._stt_http_timeout_seconds = settings.DASHSCOPE_STT_HTTP_TIMEOUT_SECONDS
        self._stt_language_hints = _parse_stt_language_hints(
            settings.DASHSCOPE_STT_LANGUAGE_HINTS
        )
        self._stt_websocket_max_attempts = _STT_WEBSOCKET_MAX_ATTEMPTS
        self._stt_websocket_retry_delay_seconds = _STT_WEBSOCKET_RETRY_DELAY_SECONDS
        self._timbre_list_doc_url = settings.DASHSCOPE_TIMBRE_LIST_DOC_URL
        self._timbre_list_cache_seconds = max(60, settings.DASHSCOPE_TIMBRE_LIST_CACHE_SECONDS)
        self._stt_ws_url = settings.DASHSCOPE_BASE_WEBSOCKET_API_URL
        self._stt_realtime_ws_url = settings.DASHSCOPE_STT_REALTIME_WEBSOCKET_API_URL
        self._tts_ws_url = settings.DASHSCOPE_TTS_WEBSOCKET_API_URL
        self._billing_region = _infer_dashscope_billing_region(
            self._stt_ws_url,
            self._stt_realtime_ws_url,
            self._tts_ws_url,
            self._tts_customization_api_url,
        )
        self._system_voices_cache: dict[str, list[dict[str, str | None]]] | None = None
        self._system_voices_cache_expire_at: float = 0.0

    def _require_api_key(self) -> str:
        if self._api_key and self._api_key.strip():
            return self._api_key.strip()
        raise APIError(
            status_code=503,
            code="voice_api_key_missing",
            message="dependency unavailable: dashscope api key is missing",
        )

    def _stt_timeout_seconds(self) -> float:
        return float(
            getattr(
                self,
                "_stt_event_timeout_seconds",
                settings.DASHSCOPE_STT_EVENT_TIMEOUT_SECONDS,
            )
        )

    def _tts_language(self) -> str:
        """Spoken language sent with every synthesis session.

        Units built via ``__new__`` (tests) do not run ``__init__``, so fall back
        to the current setting the same way ``_stt_timeout_seconds`` does.
        """
        value = getattr(self, "_tts_language_type", None) or settings.DASHSCOPE_TTS_LANGUAGE_TYPE
        return (value or "English").strip() or "English"

    def _ws_connect_factory(self) -> Callable[..., Any]:
        try:
            from websockets.asyncio.client import connect  # type: ignore

            return connect
        except Exception:
            try:
                from websockets.client import connect  # type: ignore

                return connect
            except Exception as exc:
                raise APIError(
                    status_code=503,
                    code="voice_dependency_missing",
                    message=f"dependency unavailable: websockets package is missing; err={exc}",
                )

    def _connect(
        self,
        url: str,
        *,
        api_key: str,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        connect = self._ws_connect_factory()
        kwargs = {
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 32 * 1024 * 1024,
            "open_timeout": 15,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
        }
        if extra_headers:
            headers.update(extra_headers)
        try:
            return connect(
                url,
                additional_headers=headers,
                **kwargs,
            )
        except TypeError:
            return connect(
                url,
                extra_headers=headers,
                **kwargs,
            )

    @staticmethod
    def _normalize_preferred_name(display_name: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_]+", "_", display_name.strip())
        normalized = re.sub(r"_+", "_", normalized).strip("_")
        if not normalized:
            normalized = "voice"
        if normalized[0].isdigit():
            normalized = f"v_{normalized}"

        # DashScope requires letters/numbers/underscore and max length of 16.
        random_suffix = uuid.uuid4().hex[:6]
        max_prefix_len = 16 - len(random_suffix) - 1
        prefix = normalized[:max_prefix_len] if max_prefix_len > 0 else normalized[:16]
        return f"{prefix}_{random_suffix}"[:16]

    @staticmethod
    def _audio_data_uri_for_clone(*, audio_bytes: bytes, source_audio_format: str) -> str:
        fmt = source_audio_format.strip().lower()
        mime_by_format = {
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
            "m4a": "audio/m4a",
            "flac": "audio/flac",
            "pcm": "audio/pcm",
            "opus": "audio/opus",
        }
        mime_type = mime_by_format.get(fmt)
        if not mime_type:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message=(
                    "validation failed: unsupported source audio format for dashscope clone; "
                    "supported=wav|mp3|m4a|flac|pcm|opus"
                ),
            )

        return f"data:{mime_type};base64,{base64.b64encode(audio_bytes).decode('ascii')}"

    @staticmethod
    def _extract_dashscope_error(data: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        code = data.get("code")
        message = data.get("message")
        request_id = data.get("request_id")
        return (
            code if isinstance(code, str) and code.strip() else None,
            message if isinstance(message, str) and message.strip() else None,
            request_id if isinstance(request_id, str) and request_id.strip() else None,
        )

    @staticmethod
    def _map_dashscope_http_status(status_code: int) -> int:
        if status_code in {400, 401, 403, 404, 409, 413, 415, 422, 429}:
            return status_code
        return 502

    def _raise_dashscope_http_error(
        self,
        *,
        response: httpx.Response,
        default_code: str,
        default_message: str,
    ) -> None:
        detail: dict[str, Any] = {}
        raw_text = ""
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                detail = parsed
        except Exception:
            raw_text = response.text.strip()

        upstream_code, upstream_message, upstream_request_id = self._extract_dashscope_error(detail)
        message_parts = [
            default_message,
            f"http_status={response.status_code}",
        ]
        if upstream_code:
            message_parts.append(f"upstream_code={upstream_code}")
        if upstream_message:
            message_parts.append(f"upstream_message={upstream_message}")
        elif raw_text:
            message_parts.append(f"upstream_message={raw_text[:200]}")
        if upstream_request_id:
            message_parts.append(f"request_id={upstream_request_id}")

        mapped_status = self._map_dashscope_http_status(response.status_code)
        error_code = "voice_provider_rate_limited" if mapped_status == 429 else default_code
        raise APIError(
            status_code=mapped_status,
            code=error_code,
            message="; ".join(message_parts),
        )

    async def _post_tts_customization(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self._require_api_key()
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout_seconds) as client:
                response = await client.post(
                    self._tts_customization_api_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException:
            raise APIError(
                status_code=504,
                code="external_timeout",
                message="upstream timeout: dashscope voice customization request timed out",
            )
        except httpx.HTTPError as exc:
            raise APIError(
                status_code=502,
                code="voice_provider_unavailable",
                message=f"upstream error: dashscope voice customization request failed; err={exc}",
            )

        if response.status_code != 200:
            self._raise_dashscope_http_error(
                response=response,
                default_code="voice_provider_upstream_error",
                default_message="upstream error: dashscope voice customization request failed",
            )

        try:
            data = response.json()
        except Exception as exc:
            raise APIError(
                status_code=502,
                code="voice_provider_invalid_response",
                message=(
                    "upstream error: dashscope voice customization returned invalid json; "
                    f"err={exc}"
                ),
            )
        if not isinstance(data, dict):
            raise APIError(
                status_code=502,
                code="voice_provider_invalid_response",
                message="upstream error: dashscope voice customization returned non-object response",
            )
        return data

    @staticmethod
    def _dashscope_model_alias(model: str) -> str | None:
        normalized = model.strip().lower()
        if normalized.startswith("qwen3-tts"):
            return "qwen3_tts"
        if normalized.startswith("qwen-tts"):
            return "qwen_tts"
        if normalized.startswith("cosyvoice-v3-plus"):
            return "cosyvoice_v3_plus"
        if normalized.startswith("cosyvoice-v3-flash"):
            return "cosyvoice_v3_flash"
        return None

    def _load_system_voice_map(self) -> dict[str, list[dict[str, str | None]]]:
        return dict(_QWEN3_TTS_VOICE_MAP)

    async def list_system_voices(
        self,
        *,
        provider: str,
        model: str,
    ) -> list[dict[str, str | None]]:
        provider_name = provider.strip().lower()
        if provider_name != "dashscope":
            raise APIError(
                status_code=400,
                code="voice_provider_unsupported",
                message=f"invalid request: unsupported provider; provider={provider}",
            )

        model_name = model.strip()
        if not model_name:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: model is required",
            )

        alias = self._dashscope_model_alias(model_name)
        if not alias:
            raise APIError(
                status_code=400,
                code="voice_model_unsupported",
                message=f"invalid request: unsupported tts model for voice catalog; model={model_name}",
            )

        now = time.time()
        if self._system_voices_cache is None or now >= self._system_voices_cache_expire_at:
            self._system_voices_cache = self._load_system_voice_map()
            self._system_voices_cache_expire_at = now + self._timbre_list_cache_seconds

        source = self._system_voices_cache.get(alias, [])
        result: list[dict[str, str | None]] = []
        for item in source:
            result.append(
                {
                    "provider": provider_name,
                    "provider_model": model_name,
                    "provider_voice_id": item.get("provider_voice_id"),
                    "display_name": item.get("display_name"),
                    "usage_hint": item.get("usage_hint"),
                    "preview_audio_url": item.get("preview_audio_url"),
                    "language": item.get("language"),
                    "gender": item.get("gender"),
                    "avatar_image_key": item.get("avatar_image_key"),
                }
            )
        return result

    def open_streaming_stt_session(self) -> Any | None:
        """Open a progressive ASR session for the current realtime model.

        Returns ``None`` for providers that cannot stream turn-scoped audio, so
        callers keep the one-shot path as a fallback.
        """
        from src.realtime.services.streaming_stt import open_streaming_stt

        return open_streaming_stt(repo=self)

    async def transcribe_audio_http(
        self,
        *,
        audio_bytes: bytes,
        audio_format: str,
        sample_rate: int,
        context_messages: list[dict[str, str]] | None = None,
    ) -> tuple[str, str | None]:
        """Transcribe a recorded utterance through the synchronous Qwen-Audio API.

        Only the chat composer microphone button uses this path. Realtime voice
        calls keep calling :meth:`transcribe_audio` over the realtime websocket.
        """
        api_key = self._require_api_key()
        if not audio_bytes:
            raise APIError(
                status_code=422,
                code="stt_audio_empty",
                message="invalid request: audio payload is empty",
            )

        model = getattr(self, "_stt_http_model", None) or settings.DASHSCOPE_STT_HTTP_MODEL
        api_url = (
            getattr(self, "_stt_http_api_url", None)
            or settings.DASHSCOPE_STT_HTTP_API_URL
        )
        timeout_seconds = float(
            getattr(self, "_stt_http_timeout_seconds", None)
            or settings.DASHSCOPE_STT_HTTP_TIMEOUT_SECONDS
        )
        language_hints = getattr(self, "_stt_language_hints", None)
        if language_hints is None:
            language_hints = _parse_stt_language_hints(
                settings.DASHSCOPE_STT_LANGUAGE_HINTS
            )

        audio_data_uri, upstream_format = _build_stt_http_audio(
            audio_bytes=audio_bytes,
            audio_format=audio_format,
            sample_rate=sample_rate,
        )
        messages = _build_stt_http_messages(
            audio_data_uri=audio_data_uri,
            context_messages=context_messages,
        )
        parameters: dict[str, Any] = {
            "format": upstream_format,
            "sample_rate": str(sample_rate),
        }
        if language_hints:
            parameters["language_hints"] = language_hints

        observation = _start_voice_model_observation(
            name="voice.stt.transcribe",
            model=model,
            input_payload={
                "audio_format": audio_format,
                "sample_rate": sample_rate,
                "audio_bytes": len(audio_bytes),
                "context_messages": len(messages) - 1,
            },
            metadata={
                "provider": _DASHSCOPE_PROVIDER,
                "billing_region": self._billing_region,
                "billing_unit": _LANGFUSE_UNIT_TOKENS,
                "source_unit": _LANGFUSE_UNIT_TOKENS,
                "operation": "voice.stt.transcribe",
                "model": _dashscope_model_label(model),
                "transport": "http",
            },
        )
        error_metadata = {
            "provider": _DASHSCOPE_PROVIDER,
            "billing_region": self._billing_region,
            "billing_unit": _LANGFUSE_UNIT_TOKENS,
            "source_unit": _LANGFUSE_UNIT_TOKENS,
            "audio_format": audio_format,
            "sample_rate": sample_rate,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    api_url,
                    json={
                        "model": model,
                        "input": {"messages": messages},
                        "parameters": parameters,
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "X-DashScope-SSE": "disable",
                    },
                )
        except httpx.TimeoutException as exc:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice STT HTTP request timed out",
                metadata={**error_metadata, "error_type": "TimeoutException"},
            )
            raise APIError(
                status_code=504,
                code="external_timeout",
                message="upstream timeout: dashscope stt http request timed out",
            ) from exc
        except httpx.HTTPError as exc:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice STT HTTP request failed",
                metadata={**error_metadata, "error_type": type(exc).__name__},
            )
            raise APIError(
                status_code=502,
                code="stt_upstream_error",
                message="upstream error: dashscope stt http request failed",
            ) from exc

        if response.status_code != 200:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice STT HTTP request rejected",
                metadata={**error_metadata, "upstream_status": response.status_code},
            )
            raise APIError(
                status_code=502,
                code="stt_upstream_error",
                message=(
                    "upstream error: dashscope stt http request rejected; "
                    f"status={response.status_code}"
                ),
            )

        payload = _load_json(response.text)
        request_id = payload.get("request_id")
        request_id = request_id if isinstance(request_id, str) else None
        text = _extract_stt_http_text(payload)
        usage = _extract_stt_http_usage(payload)
        if not text and not usage:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice STT HTTP response unreadable",
                metadata={**error_metadata, "error_type": "InvalidResponse"},
            )
            raise APIError(
                status_code=502,
                code="stt_upstream_error",
                message="upstream error: dashscope stt http response is unreadable",
            )
        if not text:
            _end_voice_model_observation(
                observation,
                output={"text_chars": 0, "request_id": request_id},
                metadata={**error_metadata, "request_id": request_id},
            )
            raise APIError(
                status_code=422,
                code="stt_no_speech_detected",
                message="invalid request: no speech detected in audio",
            )

        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        usage_details = (
            {"input": input_tokens, "output": output_tokens, "total": total_tokens}
            if total_tokens
            else None
        )
        _end_voice_model_observation(
            observation,
            output={"text_chars": len(text), "request_id": request_id},
            usage_details=usage_details,
            metadata={
                **error_metadata,
                "transport": "http",
                "request_id": request_id,
                "audio_duration_seconds": usage.get("duration"),
                "context_messages": len(messages) - 1,
            },
        )
        return text, request_id

    async def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        audio_format: str,
        sample_rate: int,
    ) -> tuple[str, str | None]:
        observation = _start_voice_model_observation(
            name="voice.stt.transcribe",
            model=self._stt_model,
            input_payload={
                "audio_format": audio_format,
                "sample_rate": sample_rate,
                "audio_bytes": len(audio_bytes),
            },
            metadata={
                "provider": _DASHSCOPE_PROVIDER,
                "billing_region": self._billing_region,
                "billing_unit": _LANGFUSE_UNIT_SECONDS,
                "source_unit": _LANGFUSE_UNIT_SECONDS,
                "operation": "voice.stt.transcribe",
                "model": _dashscope_model_label(self._stt_model),
            },
        )
        prepared_audio: bytes | None = None
        prepared_format: str | None = None
        try:
            if _is_qwen3_realtime_asr_model(self._stt_model):
                prepared_format, prepared_audio = self._prepare_qwen3_audio(
                    audio_bytes=audio_bytes,
                    audio_format=audio_format,
                    sample_rate=sample_rate,
                )
                text, request_id = await self._transcribe_audio_qwen3_realtime(
                    audio_bytes=audio_bytes,
                    audio_format=audio_format,
                    sample_rate=sample_rate,
                    prepared_audio=(prepared_format, prepared_audio),
                )
            else:
                text, request_id = await self._transcribe_audio_fun_asr(
                    audio_bytes=audio_bytes,
                    audio_format=audio_format,
                    sample_rate=sample_rate,
                )

            duration_seconds, duration_source = _estimate_audio_duration_seconds(
                audio_bytes=audio_bytes,
                audio_format=audio_format,
                sample_rate=sample_rate,
                prepared_audio_bytes=prepared_audio,
                prepared_audio_format=prepared_format,
            )
            usage_details = (
                _seconds_usage_details(duration_seconds)
                if duration_seconds is not None
                else None
            )
            if duration_seconds is None:
                logger.warning(
                    "STT billing usage is unavailable for audio_format=%s sample_rate=%s.",
                    audio_format,
                    sample_rate,
                    extra={
                        "log_module": "voice",
                        "event": "voice.stt.billing_usage_unavailable",
                        "provider": _DASHSCOPE_PROVIDER,
                        "model": _dashscope_model_label(self._stt_model),
                        "audio_format": audio_format,
                        "sample_rate": sample_rate,
                    },
                )
            _end_voice_model_observation(
                observation,
                output={
                    "text_chars": len(text),
                    "request_id": request_id,
                },
                usage_details=usage_details,
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_SECONDS,
                    "usage_source": duration_source,
                    "source_unit": _LANGFUSE_UNIT_SECONDS,
                    "audio_duration_seconds": (
                        round(duration_seconds, 3)
                        if duration_seconds is not None
                        else None
                    ),
                    "audio_format": audio_format,
                    "prepared_audio_format": prepared_format,
                    "sample_rate": sample_rate,
                    "request_id": request_id,
                },
            )
            return text, request_id
        except Exception as exc:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message=f"Voice STT failed: {type(exc).__name__}",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_SECONDS,
                    "source_unit": _LANGFUSE_UNIT_SECONDS,
                    "error_type": type(exc).__name__,
                    "audio_format": audio_format,
                    "sample_rate": sample_rate,
                },
            )
            raise

    async def _transcribe_audio_fun_asr(
        self,
        *,
        audio_bytes: bytes,
        audio_format: str,
        sample_rate: int,
    ) -> tuple[str, str | None]:
        api_key = self._require_api_key()
        if not audio_bytes:
            raise APIError(
                status_code=422,
                code="stt_audio_empty",
                message="invalid request: audio payload is empty",
            )

        task_id = str(uuid.uuid4())
        texts: list[str] = []
        request_id: str | None = None

        run_task_event = {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self._stt_model,
                "parameters": {
                    "format": audio_format,
                    "sample_rate": sample_rate,
                },
                "input": {},
            },
        }
        finish_task_event = {
            "header": {
                "action": "finish-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "input": {},
            },
        }

        try:
            async with self._connect(self._stt_ws_url, api_key=api_key) as ws:
                await ws.send(_json_dumps(run_task_event))

                # Wait for explicit task-started before pushing binary frames.
                while True:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=self._stt_timeout_seconds(),
                    )
                    if isinstance(raw, bytes):
                        continue
                    event = _load_json(raw)
                    header = event.get("header")
                    if isinstance(header, dict):
                        request_id = header.get("request_id") or request_id
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        request_id = payload.get("request_id") or request_id

                    event_name = None
                    if isinstance(header, dict):
                        event_name = header.get("event")
                    if not isinstance(event_name, str):
                        event_name = event.get("type")

                    if event_name in {"task-failed", "error"}:
                        raise APIError(
                            status_code=502,
                            code="stt_upstream_error",
                            message=_extract_stt_error_message(
                                event,
                                "upstream error: stt task failed",
                            ),
                        )
                    if event_name in {"task-started", "task-running"}:
                        break

                frame_size = 3200 if sample_rate == 16000 else 1600
                for i in range(0, len(audio_bytes), frame_size):
                    await ws.send(audio_bytes[i : i + frame_size])

                await ws.send(_json_dumps(finish_task_event))

                while True:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=self._stt_timeout_seconds(),
                    )
                    if isinstance(raw, bytes):
                        continue
                    event = _load_json(raw)
                    header = event.get("header")
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        request_id = payload.get("request_id") or request_id

                    event_name = None
                    if isinstance(header, dict):
                        event_name = header.get("event")
                    if not isinstance(event_name, str):
                        event_name = event.get("type")

                    if event_name in {"task-failed", "error"}:
                        raise APIError(
                            status_code=502,
                            code="stt_upstream_error",
                            message=_extract_stt_error_message(
                                event,
                                "upstream error: stt task failed",
                            ),
                        )

                    if isinstance(payload, dict):
                        text = _extract_stt_text(payload)
                        _merge_incremental_text(texts, text)

                    if event_name in {"task-finished", "task-completed", "result-finished"}:
                        break

                final_text = " ".join([item for item in texts if item]).strip()
                if not final_text:
                    raise APIError(
                        status_code=422,
                        code="stt_no_speech_detected",
                        message="seems silent: no speech detected from audio",
                    )
                return final_text, request_id
        except APIError:
            raise
        except TimeoutError:
            raise APIError(
                status_code=504,
                code="external_timeout",
                message="upstream timeout: stt service response timed out",
            )
        except Exception as exc:
            logger.exception("STT websocket call failed: %s", exc)
            raise APIError(
                status_code=502,
                code="stt_upstream_error",
                message=f"upstream error: stt service request failed; err={exc}",
            )

    def _wav_to_pcm16(
        self,
        *,
        audio_bytes: bytes,
        sample_rate: int,
    ) -> bytes:
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
                channels = wav_file.getnchannels()
                width = wav_file.getsampwidth()
                frame_rate = wav_file.getframerate()
                compression = wav_file.getcomptype()
                frames = wav_file.readframes(wav_file.getnframes())
        except Exception as exc:
            raise APIError(
                status_code=422,
                code="stt_invalid_audio",
                message=f"invalid request: wav parsing failed; err={exc}",
            )

        if compression != "NONE":
            raise APIError(
                status_code=422,
                code="stt_invalid_audio",
                message=(
                    "validation failed: unsupported wav compression; "
                    f"compression={compression}"
                ),
            )
        if channels != 1:
            raise APIError(
                status_code=422,
                code="stt_invalid_audio",
                message=(
                    "validation failed: wav must be mono for qwen3 stt; "
                    f"channels={channels}"
                ),
            )
        if width != 2:
            raise APIError(
                status_code=422,
                code="stt_invalid_audio",
                message=(
                    "validation failed: wav must be 16-bit pcm; "
                    f"sample_width_bytes={width}"
                ),
            )
        if frame_rate != sample_rate:
            raise APIError(
                status_code=422,
                code="stt_invalid_audio",
                message=(
                    "validation failed: wav sample_rate mismatch; "
                    f"expected={sample_rate}; actual={frame_rate}"
                ),
            )
        if not frames:
            raise APIError(
                status_code=422,
                code="stt_audio_empty",
                message="invalid request: audio payload is empty",
            )
        return frames

    def _prepare_qwen3_audio(
        self,
        *,
        audio_bytes: bytes,
        audio_format: str,
        sample_rate: int,
    ) -> tuple[str, bytes]:
        normalized = audio_format.strip().lower()
        if normalized == "wav":
            return "pcm", self._wav_to_pcm16(audio_bytes=audio_bytes, sample_rate=sample_rate)
        if normalized in {"pcm", "opus"}:
            return normalized, audio_bytes
        raise APIError(
            status_code=422,
            code="validation_failed",
            message=(
                "validation failed: unsupported audio format for qwen3 stt; "
                f"audio_format={audio_format}; supported=pcm|wav|opus"
            ),
        )

    async def _wait_stt_qwen3_session_updated(self, ws: Any) -> None:
        while True:
            raw = await asyncio.wait_for(
                ws.recv(),
                timeout=self._stt_timeout_seconds(),
            )
            if isinstance(raw, bytes):
                continue
            event = _load_json(raw)
            event_type = _extract_event_type(event)
            if event_type is None:
                continue
            if event_type in {"session.updated", "session.created"}:
                return
            if event_type in {"error", "response.error"}:
                message = _extract_event_message(
                    event,
                    "upstream error: stt session init failed",
                )
                raise APIError(
                    status_code=502,
                    code="stt_upstream_error",
                    message=message,
                )

    async def _transcribe_audio_qwen3_realtime(
        self,
        *,
        audio_bytes: bytes,
        audio_format: str,
        sample_rate: int,
        prepared_audio: tuple[str, bytes] | None = None,
    ) -> tuple[str, str | None]:
        api_key = self._require_api_key()
        if not audio_bytes:
            raise APIError(
                status_code=422,
                code="stt_audio_empty",
                message="invalid request: audio payload is empty",
            )

        if prepared_audio is not None:
            input_audio_format, payload_audio = prepared_audio
        else:
            input_audio_format, payload_audio = self._prepare_qwen3_audio(
                audio_bytes=audio_bytes,
                audio_format=audio_format,
                sample_rate=sample_rate,
            )

        ws_url = _append_or_replace_query_param(
            self._stt_realtime_ws_url,
            key="model",
            value=self._stt_model,
        )
        texts: list[str] = []
        request_id: str | None = None
        session_payload = {
            "event_id": _new_event_id(),
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": input_audio_format,
                "sample_rate": sample_rate,
                "turn_detection": None,
            },
        }
        commit_payload = {
            "event_id": _new_event_id(),
            "type": "input_audio_buffer.commit",
        }
        finish_payload = {
            "event_id": _new_event_id(),
            "type": "session.finish",
        }
        frame_size = 3200 if input_audio_format == "pcm" and sample_rate == 16000 else 4096
        max_attempts = max(
            1,
            int(getattr(self, "_stt_websocket_max_attempts", _STT_WEBSOCKET_MAX_ATTEMPTS)),
        )
        retry_delay_seconds = max(
            0.0,
            float(
                getattr(
                    self,
                    "_stt_websocket_retry_delay_seconds",
                    _STT_WEBSOCKET_RETRY_DELAY_SECONDS,
                )
            ),
        )

        for attempt in range(1, max_attempts + 1):
            texts.clear()
            request_id = None
            try:
                async with self._connect(
                    ws_url,
                    api_key=api_key,
                    extra_headers={"OpenAI-Beta": "realtime=v1"},
                ) as ws:
                    await ws.send(_json_dumps(session_payload))
                    await self._wait_stt_qwen3_session_updated(ws)

                    for i in range(0, len(payload_audio), frame_size):
                        chunk = payload_audio[i : i + frame_size]
                        if not chunk:
                            continue
                        append_payload = {
                            "event_id": _new_event_id(),
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode("ascii"),
                        }
                        await ws.send(_json_dumps(append_payload))

                    await ws.send(_json_dumps(commit_payload))
                    await ws.send(_json_dumps(finish_payload))

                    while True:
                        raw = await asyncio.wait_for(
                            ws.recv(),
                            timeout=self._stt_timeout_seconds(),
                        )
                        if isinstance(raw, bytes):
                            continue

                        event = _load_json(raw)
                        event_type = _extract_event_type(event)
                        if event_type is None:
                            continue

                        request_id = (
                            event.get("request_id")
                            if isinstance(event.get("request_id"), str)
                            else request_id
                        )

                        if event_type in {"error", "response.error"}:
                            message = _extract_event_message(
                                event,
                                "upstream error: stt task failed",
                            )
                            raise APIError(
                                status_code=502,
                                code="stt_upstream_error",
                                message=message,
                            )

                        if event_type in {
                            "conversation.item.input_audio_transcription.delta",
                            "conversation.item.input_audio_transcription.text",
                            "conversation.item.input_audio_transcription.completed",
                        }:
                            _merge_incremental_text(
                                texts,
                                _extract_qwen3_transcription_text(event),
                            )

                        if event_type in {"response.done", "session.finished"}:
                            _merge_incremental_text(
                                texts,
                                _extract_qwen3_transcription_text(event),
                            )
                            if event_type == "session.finished":
                                break

                    final_text = " ".join([item for item in texts if item]).strip()
                    if not final_text:
                        raise APIError(
                            status_code=422,
                            code="stt_no_speech_detected",
                            message="seems silent: no speech detected from audio",
                        )
                    return final_text, request_id
            except APIError:
                raise
            except (TimeoutError, Exception) as exc:
                is_transient = isinstance(exc, TimeoutError) or _is_transient_stt_websocket_error(exc)
                if attempt < max_attempts and is_transient:
                    logger.warning(
                        "STT websocket transient failure; retrying: %s; "
                        "configured_stt_model=%s; ws_url=%s; attempt=%s; max_attempts=%s",
                        exc,
                        self._stt_model,
                        ws_url,
                        attempt,
                        max_attempts,
                        extra={
                            "log_module": "voice",
                            "event": "voice.stt.websocket_retry",
                            "provider": _DASHSCOPE_PROVIDER,
                            "model": _dashscope_model_label(self._stt_model),
                        },
                    )
                    if retry_delay_seconds > 0:
                        await asyncio.sleep(retry_delay_seconds)
                    continue

                if isinstance(exc, TimeoutError):
                    raise APIError(
                        status_code=504,
                        code="external_timeout",
                        message="upstream timeout: stt service response timed out",
                    )

                logger.exception(
                    "STT websocket call failed: %s; configured_stt_model=%s; ws_url=%s",
                    exc,
                    self._stt_model,
                    ws_url,
                )
                raise APIError(
                    status_code=502,
                    code="stt_upstream_error",
                    message=f"upstream error: stt service request failed; err={exc}",
                )

        raise APIError(
            status_code=502,
            code="stt_upstream_error",
            message="upstream error: stt service request failed",
        )

    async def stream_tts_audio_base64(
        self,
        *,
        text: str,
        audio_format: str,
        instructions: str | None = None,
        provider_voice_id: str | None = None,
        provider_model: str | None = None,
        voice_profile: dict[str, Any] | None = None,
        session_overrides: dict[str, Any] | None = None,
    ) -> AsyncGenerator[tuple[str, str], None]:
        api_key = self._require_api_key()
        if not text.strip():
            raise APIError(
                status_code=422,
                code="tts_text_empty",
                message="invalid request: tts text is empty",
            )

        if audio_format == "opus":
            mime_type = "audio/opus"
        elif audio_format == "mp3":
            mime_type = "audio/mpeg"
        elif audio_format == "wav":
            mime_type = "audio/wav"
        elif audio_format == "pcm":
            mime_type = "audio/pcm;rate=24000"
        else:
            mime_type = "application/octet-stream"
        session_payload: dict[str, Any] = {
            "event_id": _new_event_id(),
            "type": "session.update",
            "session": {
                "voice": (provider_voice_id.strip() if provider_voice_id and provider_voice_id.strip() else "Cherry"),
                "mode": "server_commit",
                "language_type": self._tts_language(),
                "response_format": audio_format,
            },
        }
        if instructions and instructions.strip():
            session_payload["session"]["instructions"] = instructions.strip()
        session_payload["session"].update(_voice_profile_session_params(voice_profile))
        # The tuning console (tools/voice-tuner) explores the full provider range,
        # which is wider than the per-character profile bounds applied above.
        if session_overrides:
            session_payload["session"].update(session_overrides)

        append_payload = {
            "event_id": _new_event_id(),
            "type": "input_text_buffer.append",
            "text": text,
        }
        finish_payload = {
            "event_id": _new_event_id(),
            "type": "session.finish",
        }
        tts_model = provider_model.strip() if provider_model and provider_model.strip() else self._tts_model
        ws_url = _append_or_replace_query_param(
            self._tts_ws_url,
            key="model",
            value=tts_model,
        )
        input_characters = len(text)
        observation = _start_voice_model_observation(
            name="voice.tts.stream",
            model=tts_model,
            input_payload={
                "text_chars": input_characters,
                "audio_format": audio_format,
                "voice": session_payload["session"]["voice"],
            },
            metadata={
                "provider": _DASHSCOPE_PROVIDER,
                "billing_region": self._billing_region,
                "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                "operation": "voice.tts.stream",
                "model": _dashscope_model_label(tts_model),
                "provider_voice_id": session_payload["session"]["voice"],
                "instructions_chars": len(instructions.strip()) if instructions and instructions.strip() else 0,
                "output_billed": False,
            },
        )
        audio_delta_count = 0

        try:
            async with self._connect(ws_url, api_key=api_key) as ws:
                await ws.send(_json_dumps(session_payload))
                await self._wait_tts_session_updated(ws)
                await ws.send(_json_dumps(append_payload))
                await ws.send(_json_dumps(finish_payload))

                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    if isinstance(raw, bytes):
                        continue
                    event = _load_json(raw)
                    event_type = _extract_event_type(event)
                    if event_type is None:
                        continue

                    if event_type in {"error", "response.error"}:
                        message = _extract_event_message(
                            event,
                            "upstream error: tts synthesis failed",
                        )
                        raise APIError(
                            status_code=502,
                            code="tts_upstream_error",
                            message=message,
                        )

                    if event_type == "response.audio.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            audio_delta_count += 1
                            yield delta, mime_type
                        continue

                    if event_type in {"response.audio.done", "response.done", "session.finished"}:
                        break
                _end_voice_model_observation(
                    observation,
                    output={
                        "audio_delta_count": audio_delta_count,
                        "mime_type": mime_type,
                    },
                    usage_details=_characters_usage_details(input_characters),
                    metadata={
                        "provider": _DASHSCOPE_PROVIDER,
                        "billing_region": self._billing_region,
                        "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                        "usage_source": "input_text_characters",
                        "audio_format": audio_format,
                        "mime_type": mime_type,
                        "provider_voice_id": session_payload["session"]["voice"],
                        "instructions_chars": len(instructions.strip()) if instructions and instructions.strip() else 0,
                        "output_billed": False,
                    },
                )
        except APIError:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice TTS failed: APIError",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                    "error_type": "APIError",
                    "audio_format": audio_format,
                    "provider_voice_id": session_payload["session"]["voice"],
                },
            )
            raise
        except TimeoutError:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice TTS failed: TimeoutError",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                    "error_type": "TimeoutError",
                    "audio_format": audio_format,
                    "provider_voice_id": session_payload["session"]["voice"],
                },
            )
            raise APIError(
                status_code=504,
                code="external_timeout",
                message="upstream timeout: tts service response timed out",
            )
        except Exception as exc:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message=f"Voice TTS failed: {type(exc).__name__}",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                    "error_type": type(exc).__name__,
                    "audio_format": audio_format,
                    "provider_voice_id": session_payload["session"]["voice"],
                },
            )
            logger.exception(
                "TTS websocket call failed: %s; configured_tts_model=%s; ws_url=%s",
                exc,
                tts_model,
                ws_url,
            )
            raise APIError(
                status_code=502,
                code="tts_upstream_error",
                message=f"upstream error: tts service request failed; err={exc}",
            )

    async def create_voice_clone(
        self,
        *,
        provider: str,
        display_name: str,
        source_audio_bytes: bytes,
        source_audio_format: str,
        description: str | None = None,
        language_hint: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _ = description
        _ = idempotency_key

        provider_name = provider.strip().lower()
        if provider_name != "dashscope":
            raise APIError(
                status_code=400,
                code="voice_provider_unsupported",
                message=f"invalid request: unsupported provider; provider={provider}",
            )
        if not source_audio_bytes:
            raise APIError(
                status_code=422,
                code="voice_clone_audio_empty",
                message="invalid request: source audio payload is empty",
            )
        if not display_name.strip():
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: display_name is required",
            )

        payload_input: dict[str, Any] = {
            "action": "create",
            "target_model": self._voice_clone_target_model,
            "preferred_name": self._normalize_preferred_name(display_name),
            "audio": {
                "data": self._audio_data_uri_for_clone(
                    audio_bytes=source_audio_bytes,
                    source_audio_format=source_audio_format,
                )
            },
        }
        if language_hint and language_hint.strip():
            payload_input["language"] = language_hint.strip()

        source_duration_seconds, source_duration_method = _estimate_audio_duration_seconds(
            audio_bytes=source_audio_bytes,
            audio_format=source_audio_format,
            sample_rate=16000,
        )
        observation = _start_voice_model_observation(
            name="voice.clone.create",
            model=self._voice_enrollment_model,
            input_payload={
                "provider": _DASHSCOPE_PROVIDER,
                "operation": "voice.clone.create",
                "model": _dashscope_model_label(self._voice_enrollment_model),
                "target_model": self._voice_clone_target_model,
                "audio_format": source_audio_format,
                "audio_bytes": len(source_audio_bytes),
                "audio_duration_seconds": source_duration_seconds,
                "audio_duration_method": source_duration_method,
                "language_hint": language_hint,
            },
            metadata={
                "provider": _DASHSCOPE_PROVIDER,
                "billing_region": self._billing_region,
                "billing_unit": _LANGFUSE_UNIT_SECONDS,
                "operation": "voice.clone.create",
                "target_model": _dashscope_model_label(self._voice_clone_target_model),
            },
        )
        payload = {
            "model": self._voice_enrollment_model,
            "input": payload_input,
        }
        try:
            data = await self._post_tts_customization(payload)
        except Exception as exc:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message=f"Voice clone create failed: {type(exc).__name__}",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_SECONDS,
                    "error_type": type(exc).__name__,
                    "target_model": _dashscope_model_label(self._voice_clone_target_model),
                },
            )
            raise

        output = data.get("output")
        if not isinstance(output, dict):
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice clone create failed: missing output",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_SECONDS,
                    "target_model": _dashscope_model_label(self._voice_clone_target_model),
                },
            )
            raise APIError(
                status_code=502,
                code="voice_provider_invalid_response",
                message="upstream error: dashscope create clone response missing output",
            )

        provider_voice_id = output.get("voice")
        if not isinstance(provider_voice_id, str) or not provider_voice_id.strip():
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice clone create failed: missing output.voice",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_SECONDS,
                    "target_model": _dashscope_model_label(self._voice_clone_target_model),
                },
            )
            raise APIError(
                status_code=502,
                code="voice_provider_invalid_response",
                message="upstream error: dashscope create clone response missing output.voice",
            )

        provider_model = output.get("target_model")
        if not isinstance(provider_model, str) or not provider_model.strip():
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice clone create failed: missing output.target_model",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_SECONDS,
                    "target_model": _dashscope_model_label(self._voice_clone_target_model),
                },
            )
            raise APIError(
                status_code=502,
                code="voice_provider_invalid_response",
                message="upstream error: dashscope create clone response missing output.target_model",
            )

        provider_request_id = data.get("request_id")
        if not isinstance(provider_request_id, str):
            provider_request_id = None

        _end_voice_model_observation(
            observation,
            output={
                "provider_voice_id": provider_voice_id.strip(),
                "provider_model": provider_model,
                "provider_request_id": provider_request_id,
            },
            usage_details=(
                _seconds_usage_details(source_duration_seconds)
                if source_duration_seconds is not None and source_duration_seconds > 0
                else _requests_usage_details()
            ),
            metadata={
                "provider": _DASHSCOPE_PROVIDER,
                "billing_region": self._billing_region,
                "billing_unit": (
                    _LANGFUSE_UNIT_SECONDS
                    if source_duration_seconds is not None and source_duration_seconds > 0
                    else _LANGFUSE_UNIT_REQUESTS
                ),
                "audio_duration_method": source_duration_method,
                "request_id": provider_request_id,
                "target_model": _dashscope_model_label(provider_model),
            },
        )

        return {
            "provider_voice_id": provider_voice_id.strip(),
            "provider_status": "active",
            "provider_model": provider_model,
            "provider_request_id": provider_request_id,
            "estimated_ready_seconds": 0,
            "preview_audio_url": None,
        }

    async def delete_voice_clone(
        self,
        *,
        provider: str,
        provider_voice_id: str,
    ) -> dict[str, Any]:
        provider_name = provider.strip().lower()
        if provider_name != "dashscope":
            raise APIError(
                status_code=400,
                code="voice_provider_unsupported",
                message=f"invalid request: unsupported provider; provider={provider}",
            )

        voice_id = provider_voice_id.strip()
        if not voice_id:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: provider_voice_id is required",
            )

        payload = {
            "model": self._voice_enrollment_model,
            "input": {
                "action": "delete",
                "voice": voice_id,
            },
        }
        observation = _start_voice_model_observation(
            name="voice.clone.delete",
            model=self._voice_enrollment_model,
            input_payload={
                "provider": _DASHSCOPE_PROVIDER,
                "operation": "voice.clone.delete",
                "model": _dashscope_model_label(self._voice_enrollment_model),
                "provider_voice_id": voice_id,
            },
            metadata={
                "provider": _DASHSCOPE_PROVIDER,
                "billing_region": self._billing_region,
                "billing_unit": _LANGFUSE_UNIT_REQUESTS,
                "operation": "voice.clone.delete",
            },
        )
        try:
            data = await self._post_tts_customization(payload)
        except Exception as exc:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message=f"Voice clone delete failed: {type(exc).__name__}",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_REQUESTS,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        request_id = data.get("request_id")
        provider_request_id = request_id if isinstance(request_id, str) else None
        _end_voice_model_observation(
            observation,
            output={"provider_request_id": provider_request_id},
            usage_details=_requests_usage_details(),
            metadata={
                "provider": _DASHSCOPE_PROVIDER,
                "billing_region": self._billing_region,
                "billing_unit": _LANGFUSE_UNIT_REQUESTS,
                "request_id": provider_request_id,
            },
        )
        return {
            "provider_request_id": provider_request_id,
        }

    async def stream_tts_audio_base64_from_segments(
        self,
        *,
        segments_queue: asyncio.Queue[str | None],
        audio_format: str,
        instructions: str | None = None,
        provider_voice_id: str | None = None,
        provider_model: str | None = None,
        voice_profile: dict[str, Any] | None = None,
        debug_capture: Any | None = None,
        segment_counter: list[int] | None = None,
    ) -> AsyncGenerator[tuple[str, str], None]:
        api_key = self._require_api_key()

        if audio_format == "opus":
            mime_type = "audio/opus"
        elif audio_format == "mp3":
            mime_type = "audio/mpeg"
        elif audio_format == "pcm":
            mime_type = "audio/pcm;rate=24000"
        else:
            mime_type = "application/octet-stream"

        session_payload: dict[str, Any] = {
            "event_id": _new_event_id(),
            "type": "session.update",
            "session": {
                "voice": (provider_voice_id.strip() if provider_voice_id and provider_voice_id.strip() else "Cherry"),
                "mode": "server_commit",
                "language_type": self._tts_language(),
                "response_format": audio_format,
            },
        }
        if instructions and instructions.strip():
            session_payload["session"]["instructions"] = instructions.strip()
        session_payload["session"].update(_voice_profile_session_params(voice_profile))

        tts_model = provider_model.strip() if provider_model and provider_model.strip() else self._tts_model
        ws_url = _append_or_replace_query_param(
            self._tts_ws_url,
            key="model",
            value=tts_model,
        )
        sender_task: asyncio.Task[None] | None = None
        sent_segment_count = 0
        response_done_count = 0
        input_characters = 0
        audio_delta_count = 0
        observation = _start_voice_model_observation(
            name="voice.tts.stream_segments",
            model=tts_model,
            input_payload={
                "audio_format": audio_format,
                "voice": session_payload["session"]["voice"],
                "streamed_segments": True,
            },
            metadata={
                "provider": _DASHSCOPE_PROVIDER,
                "billing_region": self._billing_region,
                "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                "operation": "voice.tts.stream_segments",
                "model": _dashscope_model_label(tts_model),
                "provider_voice_id": session_payload["session"]["voice"],
                "instructions_chars": len(instructions.strip()) if instructions and instructions.strip() else 0,
                "output_billed": False,
            },
        )

        async def _sender(ws: Any) -> None:
            nonlocal input_characters, sent_segment_count
            while True:
                segment = await segments_queue.get()
                if segment is None:
                    break
                text = segment.strip()
                if not text:
                    continue
                input_characters += len(text)
                sent_segment_count += 1
                if debug_capture is not None:
                    debug_capture.record_tts_segment(
                        index=sent_segment_count,
                        text=text,
                    )
                append_payload = {
                    "event_id": _new_event_id(),
                    "type": "input_text_buffer.append",
                    "text": text,
                }
                await ws.send(_json_dumps(append_payload))

            finish_payload = {
                "event_id": _new_event_id(),
                "type": "session.finish",
            }
            await ws.send(_json_dumps(finish_payload))

        try:
            async with self._connect(ws_url, api_key=api_key) as ws:
                await ws.send(_json_dumps(session_payload))
                await self._wait_tts_session_updated(ws)
                sender_task = asyncio.create_task(
                    _sender(ws),
                    name="voice.tts.segment_sender",
                )

                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    if isinstance(raw, bytes):
                        continue
                    event = _load_json(raw)
                    event_type = _extract_event_type(event)
                    if event_type is None:
                        continue
                    if debug_capture is not None:
                        debug_capture.record_tts_event(
                            event_type=event_type,
                            audio_delta_chars=(
                                len(event.get("delta"))
                                if event_type == "response.audio.delta"
                                and isinstance(event.get("delta"), str)
                                else None
                            ),
                        )

                    if event_type in {"error", "response.error"}:
                        message = _extract_event_message(
                            event,
                            "upstream error: tts synthesis failed",
                        )
                        raise APIError(
                            status_code=502,
                            code="tts_upstream_error",
                            message=message,
                        )

                    if event_type == "response.audio.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            audio_delta_count += 1
                            yield delta, mime_type
                        continue

                    if event_type == "response.done":
                        response_done_count += 1
                        if sender_task is not None and sender_task.done() and response_done_count >= sent_segment_count:
                            break
                        continue

                    if event_type == "response.audio.done":
                        continue

                    if event_type == "session.finished":
                        break

                if sender_task is not None:
                    await sender_task
                _end_voice_model_observation(
                    observation,
                    output={
                        "audio_delta_count": audio_delta_count,
                        "mime_type": mime_type,
                        "segment_count": sent_segment_count,
                    },
                    usage_details=_characters_usage_details(input_characters),
                    metadata={
                        "provider": _DASHSCOPE_PROVIDER,
                        "billing_region": self._billing_region,
                        "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                        "usage_source": "input_text_characters",
                        "audio_format": audio_format,
                        "mime_type": mime_type,
                        "provider_voice_id": session_payload["session"]["voice"],
                        "instructions_chars": len(instructions.strip()) if instructions and instructions.strip() else 0,
                        "output_billed": False,
                        "segment_count": sent_segment_count,
                    },
                )
        except APIError:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice segmented TTS failed: APIError",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                    "error_type": "APIError",
                    "audio_format": audio_format,
                    "provider_voice_id": session_payload["session"]["voice"],
                    "segment_count": sent_segment_count,
                },
            )
            raise
        except TimeoutError:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message="Voice segmented TTS failed: TimeoutError",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                    "error_type": "TimeoutError",
                    "audio_format": audio_format,
                    "provider_voice_id": session_payload["session"]["voice"],
                    "segment_count": sent_segment_count,
                },
            )
            raise APIError(
                status_code=504,
                code="external_timeout",
                message="upstream timeout: tts service response timed out",
            )
        except Exception as exc:
            _end_voice_model_observation(
                observation,
                level="ERROR",
                status_message=f"Voice segmented TTS failed: {type(exc).__name__}",
                metadata={
                    "provider": _DASHSCOPE_PROVIDER,
                    "billing_region": self._billing_region,
                    "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                    "error_type": type(exc).__name__,
                    "audio_format": audio_format,
                    "provider_voice_id": session_payload["session"]["voice"],
                    "segment_count": sent_segment_count,
                },
            )
            logger.exception(
                "TTS websocket call failed: %s; configured_tts_model=%s; ws_url=%s",
                exc,
                tts_model,
                ws_url,
            )
            raise APIError(
                status_code=502,
                code="tts_upstream_error",
                message=f"upstream error: tts service request failed; err={exc}",
            )
        finally:
            if segment_counter is not None:
                segment_counter[0] = sent_segment_count
            if sender_task and not sender_task.done():
                sender_task.cancel()

    async def _wait_tts_session_updated(self, ws: Any) -> None:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            if isinstance(raw, bytes):
                continue
            event = _load_json(raw)
            event_type = _extract_event_type(event)
            if event_type is None:
                continue
            if event_type == "session.updated":
                return
            if event_type in {"error", "response.error"}:
                message = _extract_event_message(
                    event,
                    "upstream error: tts session init failed",
                )
                raise APIError(
                    status_code=502,
                    code="tts_upstream_error",
                    message=message,
                )
