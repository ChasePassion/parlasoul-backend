"""Provider-aware TTS gateway service."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from src.core.exceptions import APIError
from src.core.tts_registry import ResolvedTTSRoute, TTSAudioProfile, TTSRegistry, tts_registry
from src.core.tts_settings import tts_settings
from src.core.tts_voice_catalog import (
    DEFAULT_DASHSCOPE_SYSTEM_VOICE_ID,
    DEFAULT_MINIMAX_SYSTEM_VOICE_ID,
    legacy_dashscope_voice_id_for_minimax,
)
from src.observability import (
    VoiceCallRequest,
    finish_voice_generation,
    mark_voice_audio_delta,
    record_voice_generation_failure,
    start_voice_generation,
)
from src.repositories.voice_gateway_repository import VoiceGatewayRepository

logger = logging.getLogger(__name__)

_LANGFUSE_UNIT_CHARACTERS = "CHARACTERS"


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _load_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _event_name(payload: dict[str, Any]) -> str | None:
    for key in ("event", "type"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _characters_usage_details(input_characters: int) -> dict[str, Any]:
    return {
        "input": max(0, input_characters),
        "total": max(0, input_characters),
        "unit": _LANGFUSE_UNIT_CHARACTERS,
    }


def _extract_minimax_error(payload: dict[str, Any]) -> tuple[int | None, str | None, str | None]:
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict):
        status_code = base_resp.get("status_code")
        status_msg = base_resp.get("status_msg")
        return (
            status_code if isinstance(status_code, int) else None,
            str(status_msg) if status_msg is not None else None,
            None,
        )
    code = payload.get("code")
    message = payload.get("message") or payload.get("msg")
    return (
        None,
        str(message) if message is not None else None,
        str(code) if code is not None else None,
    )


@dataclass(frozen=True, slots=True)
class _MinimaxWarmKey:
    websocket_url: str
    provider: str
    model: str
    provider_voice_id: str
    audio_format: str
    sample_rate: int | None
    channels: int | None
    bitrate: int | None


@dataclass(slots=True)
class _MinimaxWarmConnection:
    key: _MinimaxWarmKey
    context_manager: Any
    ws: Any
    created_at: float
    ready_at: float
    expires_at: float


@dataclass(slots=True)
class _MinimaxWarmEntry:
    key: _MinimaxWarmKey
    task: asyncio.Task[_MinimaxWarmConnection]
    created_at: float
    expires_at: float
    route: ResolvedTTSRoute
    audio_profile: TTSAudioProfile
    provider_voice_id: str
    api_key: str


def _warm_key_label(key: _MinimaxWarmKey) -> str:
    return f"{key.provider}/{key.model}/{key.provider_voice_id}/{key.audio_format}"


class TTSGatewayService:
    """Route TTS calls to the configured provider transport."""

    def __init__(
        self,
        *,
        registry: TTSRegistry | None = None,
        dashscope_gateway: VoiceGatewayRepository | None = None,
    ) -> None:
        self._registry = registry or tts_registry
        self._dashscope_gateway = dashscope_gateway or VoiceGatewayRepository()
        self._minimax_event_timeout_seconds = float(tts_settings.MINIMAX_TTS_EVENT_TIMEOUT_SECONDS)
        self._minimax_open_timeout_seconds = float(tts_settings.MINIMAX_TTS_OPEN_TIMEOUT_SECONDS)
        self._minimax_warm_enabled = bool(tts_settings.MINIMAX_TTS_WARM_CONNECTION_ENABLED)
        self._minimax_warm_ttl_seconds = max(
            1.0,
            float(tts_settings.MINIMAX_TTS_WARM_CONNECTION_TTL_SECONDS),
        )
        self._minimax_warm_wait_seconds = max(
            0.0,
            float(tts_settings.MINIMAX_TTS_WARM_CONNECTION_WAIT_SECONDS),
        )
        self._minimax_warm_keepalive_seconds = max(
            self._minimax_warm_ttl_seconds,
            float(tts_settings.MINIMAX_TTS_WARM_CONNECTION_KEEPALIVE_SECONDS),
        )
        self._minimax_warm_refresh_margin_seconds = min(
            max(
                1.0,
                float(tts_settings.MINIMAX_TTS_WARM_CONNECTION_REFRESH_MARGIN_SECONDS),
            ),
            max(1.0, self._minimax_warm_ttl_seconds - 1.0),
        )
        self._minimax_warm_health_check_interval_seconds = min(
            max(
                1.0,
                float(tts_settings.MINIMAX_TTS_WARM_HEALTH_CHECK_INTERVAL_SECONDS),
            ),
            self._minimax_warm_ttl_seconds,
        )
        self._minimax_warm_connections: dict[_MinimaxWarmKey, _MinimaxWarmEntry] = {}
        self._minimax_warm_keepalive_until: dict[_MinimaxWarmKey, float] = {}
        self._minimax_warm_lock = asyncio.Lock()
        self._minimax_warm_replenish_tasks: set[asyncio.Task[None]] = set()
        self._minimax_warm_maintenance_tasks: set[asyncio.Task[None]] = set()

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

    def _connect(self, url: str, *, api_key: str) -> Any:
        connect = self._ws_connect_factory()
        kwargs = {
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 32 * 1024 * 1024,
            "open_timeout": self._minimax_open_timeout_seconds,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            return connect(url, additional_headers=headers, **kwargs)
        except TypeError:
            return connect(url, extra_headers=headers, **kwargs)

    @staticmethod
    def _require_api_key(route: ResolvedTTSRoute) -> str:
        if route.api_key and route.api_key.strip():
            return route.api_key.strip()
        raise APIError(
            status_code=503,
            code="voice_api_key_missing",
            message=f"dependency unavailable: {route.provider} tts api key is missing",
        )

    @staticmethod
    def _voice_id(provider_voice_id: str | None) -> str:
        candidate = (provider_voice_id or "").strip()
        return candidate or DEFAULT_MINIMAX_SYSTEM_VOICE_ID

    def _normalize_system_voice_inputs(
        self,
        *,
        provider: str,
        provider_model: str | None,
        provider_voice_id: str | None,
        voice_source_type: str,
    ) -> tuple[str, str | None, str | None]:
        """Route legacy MiniMax system voices through the active provider.

        Users keep the voice they selected (the MiniMax catalog and the
        DashScope catalog expose the same 19 characters).  When the system
        provider is DashScope, translate the stored MiniMax voice id to its
        Qwen-TTS counterpart instead of requiring a database rewrite.
        """
        normalized_source = (voice_source_type or "").strip().lower()
        normalized_provider = (provider or "").strip().lower()
        if (
            normalized_source == "system"
            and normalized_provider == "minimax"
            and self._registry.router.system_provider == "dashscope"
        ):
            mapped = legacy_dashscope_voice_id_for_minimax(
                (provider_voice_id or "").strip()
            )
            return (
                "dashscope",
                None,
                mapped or DEFAULT_DASHSCOPE_SYSTEM_VOICE_ID,
            )
        return provider, provider_model, provider_voice_id

    def _resolve_route_and_profile(
        self,
        *,
        provider: str,
        provider_model: str | None,
        voice_source_type: str,
        audio_format: str,
    ) -> tuple[ResolvedTTSRoute, TTSAudioProfile]:
        route = self._registry.resolve_explicit_route(
            provider=provider,
            model=provider_model,
            source_type=voice_source_type,
        )
        audio_profile = self._registry.resolve_audio_profile(
            provider=route.provider,
            model=route.model,
            audio_format=audio_format,
            stream_required=True,
        )
        return route, audio_profile

    async def stream_tts_audio_base64(
        self,
        *,
        text: str,
        audio_format: str,
        provider: str,
        provider_voice_id: str | None,
        provider_model: str | None,
        voice_source_type: str,
        instructions: str | None = None,
        voice_profile: dict[str, Any] | None = None,
    ) -> AsyncGenerator[tuple[str, str], None]:
        provider, provider_model, provider_voice_id = self._normalize_system_voice_inputs(
            provider=provider,
            provider_model=provider_model,
            provider_voice_id=provider_voice_id,
            voice_source_type=voice_source_type,
        )
        route, audio_profile = self._resolve_route_and_profile(
            provider=provider,
            provider_model=provider_model,
            voice_source_type=voice_source_type,
            audio_format=audio_format,
        )
        if route.provider == "dashscope":
            async for chunk in self._dashscope_gateway.stream_tts_audio_base64(
                text=text,
                audio_format=audio_format,
                instructions=instructions,
                provider_voice_id=provider_voice_id,
                provider_model=route.model,
                voice_profile=voice_profile,
            ):
                yield chunk
            return

        if route.provider == "minimax":
            async for chunk in self._stream_minimax_text(
                route=route,
                text=text,
                audio_profile=audio_profile,
                provider_voice_id=provider_voice_id,
            ):
                yield chunk
            return

        raise APIError(
            status_code=400,
            code="voice_provider_unsupported",
            message=f"invalid request: unsupported provider; provider={route.provider}",
        )

    async def stream_tts_audio_base64_from_segments(
        self,
        *,
        segments_queue: asyncio.Queue[str | None],
        audio_format: str,
        provider: str,
        provider_voice_id: str | None,
        provider_model: str | None,
        voice_source_type: str,
        instructions: str | None = None,
        voice_profile: dict[str, Any] | None = None,
        debug_capture: Any | None = None,
        segment_counter: list[int] | None = None,
    ) -> AsyncGenerator[tuple[str, str], None]:
        provider, provider_model, provider_voice_id = self._normalize_system_voice_inputs(
            provider=provider,
            provider_model=provider_model,
            provider_voice_id=provider_voice_id,
            voice_source_type=voice_source_type,
        )
        route, audio_profile = self._resolve_route_and_profile(
            provider=provider,
            provider_model=provider_model,
            voice_source_type=voice_source_type,
            audio_format=audio_format,
        )
        if route.provider == "dashscope":
            async for chunk in self._dashscope_gateway.stream_tts_audio_base64_from_segments(
                segments_queue=segments_queue,
                audio_format=audio_format,
                instructions=instructions,
                provider_voice_id=provider_voice_id,
                provider_model=route.model,
                voice_profile=voice_profile,
                debug_capture=debug_capture,
                segment_counter=segment_counter,
            ):
                yield chunk
            return

        if route.provider == "minimax":
            async for chunk in self._stream_minimax_segments(
                route=route,
                segments_queue=segments_queue,
                audio_profile=audio_profile,
                provider_voice_id=provider_voice_id,
                debug_capture=debug_capture,
                segment_counter=segment_counter,
            ):
                yield chunk
            return

        raise APIError(
            status_code=400,
            code="voice_provider_unsupported",
            message=f"invalid request: unsupported provider; provider={route.provider}",
        )

    async def warm_tts_audio_base64_from_segments(
        self,
        *,
        audio_format: str,
        provider: str,
        provider_voice_id: str | None,
        provider_model: str | None,
        voice_source_type: str,
    ) -> bool:
        """Best-effort MiniMax segmented TTS websocket warm-up.

        The warmed websocket has already completed `task_start`; it is single-use
        and can be acquired by the next segmented stream with the same route.
        """
        if not self._minimax_warm_enabled:
            return False

        provider, provider_model, provider_voice_id = self._normalize_system_voice_inputs(
            provider=provider,
            provider_model=provider_model,
            provider_voice_id=provider_voice_id,
            voice_source_type=voice_source_type,
        )

        try:
            route, audio_profile = self._resolve_route_and_profile(
                provider=provider,
                provider_model=provider_model,
                voice_source_type=voice_source_type,
                audio_format=audio_format,
            )
            if route.provider != "minimax":
                return False
            voice_id = self._voice_id(provider_voice_id)
            api_key = self._require_api_key(route)
        except APIError as exc:
            logger.warning(
                "MiniMax TTS warm-up skipped.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_skipped",
                    "operation": "voice.tts.warm_segments",
                    "provider": provider,
                    "model": provider_model,
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_format,
                    "warm_status": exc.code,
                },
            )
            return False

        entry = await self._ensure_minimax_warm_connection(
            route=route,
            audio_profile=audio_profile,
            provider_voice_id=voice_id,
            api_key=api_key,
        )
        if entry is None:
            return False

        try:
            await asyncio.shield(entry.task)
        except Exception:
            return False
        return True

    def _minimax_task_start_payload(
        self,
        *,
        route: ResolvedTTSRoute,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str,
    ) -> dict[str, Any]:
        audio_setting: dict[str, Any] = {
            "sample_rate": audio_profile.sample_rate or tts_settings.MINIMAX_TTS_SAMPLE_RATE,
            "format": audio_profile.format,
            "channel": audio_profile.channels or tts_settings.MINIMAX_TTS_CHANNEL,
        }
        if audio_profile.format == "mp3" and audio_profile.bitrate:
            audio_setting["bitrate"] = audio_profile.bitrate
        return {
            "event": "task_start",
            "model": route.model,
            "voice_setting": {
                "voice_id": provider_voice_id,
                "speed": float(tts_settings.MINIMAX_TTS_DEFAULT_SPEED),
                "vol": float(tts_settings.MINIMAX_TTS_DEFAULT_VOLUME),
                "pitch": int(tts_settings.MINIMAX_TTS_DEFAULT_PITCH),
            },
            "audio_setting": audio_setting,
        }

    async def _wait_for_minimax_event(
        self,
        ws: Any,
        *,
        expected_events: set[str],
        operation: str,
    ) -> dict[str, Any]:
        while True:
            raw = await asyncio.wait_for(
                ws.recv(),
                timeout=self._minimax_event_timeout_seconds,
            )
            if isinstance(raw, bytes):
                continue
            payload = _load_json(raw)
            event = _event_name(payload)
            self._raise_for_minimax_error(payload, default_message=f"{operation} failed")
            if event in expected_events:
                return payload

    @staticmethod
    def _raise_for_minimax_error(payload: dict[str, Any], *, default_message: str) -> None:
        event = _event_name(payload)
        status_code, status_message, upstream_code = _extract_minimax_error(payload)
        if status_code in (None, 0) and event not in {"error", "task_failed"}:
            return
        message_parts = [f"upstream error: minimax tts {default_message}"]
        if status_code is not None:
            message_parts.append(f"upstream_status={status_code}")
        if upstream_code:
            message_parts.append(f"upstream_code={upstream_code}")
        if status_message:
            message_parts.append(f"upstream_message={status_message}")
        raise APIError(
            status_code=502,
            code="tts_upstream_error",
            message="; ".join(message_parts),
        )

    @staticmethod
    def _decode_minimax_audio_hex(payload: dict[str, Any]) -> bytes | None:
        data = payload.get("data")
        audio_hex = data.get("audio") if isinstance(data, dict) else None
        if audio_hex is None:
            return None
        if not isinstance(audio_hex, str):
            raise APIError(
                status_code=502,
                code="voice_provider_invalid_response",
                message="upstream error: minimax tts returned non-string audio chunk",
            )
        if not audio_hex:
            return None
        try:
            return bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise APIError(
                status_code=502,
                code="voice_provider_invalid_response",
                message="upstream error: minimax tts returned invalid hex audio chunk",
            ) from exc

    async def _start_minimax_task(
        self,
        ws: Any,
        *,
        route: ResolvedTTSRoute,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str,
    ) -> None:
        await self._wait_for_minimax_event(
            ws,
            expected_events={"connected_success"},
            operation="connection",
        )
        await ws.send(
            _json_dumps(
                self._minimax_task_start_payload(
                    route=route,
                    audio_profile=audio_profile,
                    provider_voice_id=provider_voice_id,
                )
            )
        )
        await self._wait_for_minimax_event(
            ws,
            expected_events={"task_started"},
            operation="task start",
        )

    @staticmethod
    def _minimax_warm_key(
        *,
        route: ResolvedTTSRoute,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str,
    ) -> _MinimaxWarmKey:
        return _MinimaxWarmKey(
            websocket_url=route.websocket_url,
            provider=route.provider,
            model=route.model,
            provider_voice_id=provider_voice_id,
            audio_format=audio_profile.format,
            sample_rate=audio_profile.sample_rate or tts_settings.MINIMAX_TTS_SAMPLE_RATE,
            channels=audio_profile.channels or tts_settings.MINIMAX_TTS_CHANNEL,
            bitrate=audio_profile.bitrate,
        )

    async def _ensure_minimax_warm_connection(
        self,
        *,
        route: ResolvedTTSRoute,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str,
        api_key: str,
    ) -> _MinimaxWarmEntry | None:
        if not self._minimax_warm_enabled:
            return None

        key = self._minimax_warm_key(
            route=route,
            audio_profile=audio_profile,
            provider_voice_id=provider_voice_id,
        )
        now = time.monotonic()
        keepalive_until = now + self._minimax_warm_keepalive_seconds
        expired_entry: _MinimaxWarmEntry | None = None
        async with self._minimax_warm_lock:
            self._minimax_warm_keepalive_until[key] = max(
                keepalive_until,
                self._minimax_warm_keepalive_until.get(key, 0.0),
            )
            existing = self._minimax_warm_connections.get(key)
            if existing is not None:
                if now >= existing.expires_at:
                    expired_entry = self._minimax_warm_connections.pop(key)
                elif existing.task.done():
                    if existing.task.cancelled():
                        self._minimax_warm_connections.pop(key, None)
                    else:
                        with suppress(Exception):
                            existing.task.result()
                            return existing
                        self._minimax_warm_connections.pop(key, None)
                else:
                    return existing

            expires_at = now + self._minimax_warm_ttl_seconds
            task = asyncio.create_task(
                self._create_minimax_warm_connection(
                    key=key,
                    route=route,
                    audio_profile=audio_profile,
                    provider_voice_id=provider_voice_id,
                    api_key=api_key,
                    created_at=now,
                    expires_at=expires_at,
                ),
                name="voice.tts.minimax_warm_connection",
            )
            entry = _MinimaxWarmEntry(
                key=key,
                task=task,
                created_at=now,
                expires_at=expires_at,
                route=route,
                audio_profile=audio_profile,
                provider_voice_id=provider_voice_id,
                api_key=api_key,
            )
            self._minimax_warm_connections[key] = entry
            task.add_done_callback(
                lambda done_task: self._on_minimax_warm_task_done(key, done_task)
            )
            self._schedule_minimax_warm_maintenance(entry)

        if expired_entry is not None:
            await self._close_minimax_warm_entry(expired_entry)

        logger.info(
            "MiniMax TTS warm connection started.",
            extra={
                "log_module": "voice",
                "event": "voice.tts.warm_start",
                "operation": "voice.tts.warm_segments",
                "provider": route.provider,
                "model": f"{route.provider}/{route.model}",
                "provider_voice_id": provider_voice_id,
                "audio_format": audio_profile.format,
                "warm_key": _warm_key_label(key),
                "ttl_ms": int(self._minimax_warm_ttl_seconds * 1000),
                "warm_keepalive_ms": int((keepalive_until - now) * 1000),
                "warm_refresh_in_ms": int(
                    max(
                        0.0,
                        self._minimax_warm_ttl_seconds - self._minimax_warm_refresh_margin_seconds,
                    )
                    * 1000
                ),
            },
        )
        return entry

    def _schedule_minimax_warm_maintenance(self, entry: _MinimaxWarmEntry) -> None:
        task = asyncio.create_task(
            self._maintain_minimax_warm_entry(entry),
            name="voice.tts.minimax_warm_maintenance",
        )
        self._minimax_warm_maintenance_tasks.add(task)
        task.add_done_callback(self._minimax_warm_maintenance_tasks.discard)

    def _schedule_minimax_warm_replenishment(
        self,
        *,
        route: ResolvedTTSRoute,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str,
        api_key: str,
        voice_call_id: str,
        source: str,
    ) -> None:
        if not self._minimax_warm_enabled:
            return

        key = self._minimax_warm_key(
            route=route,
            audio_profile=audio_profile,
            provider_voice_id=provider_voice_id,
        )
        task = asyncio.create_task(
            self._replenish_minimax_warm_connection(
                route=route,
                audio_profile=audio_profile,
                provider_voice_id=provider_voice_id,
                api_key=api_key,
                key=key,
                voice_call_id=voice_call_id,
                source=source,
            ),
            name="voice.tts.minimax_warm_replenish",
        )
        self._minimax_warm_replenish_tasks.add(task)
        task.add_done_callback(self._minimax_warm_replenish_tasks.discard)
        logger.info(
            "MiniMax TTS warm connection replenish scheduled.",
            extra={
                "log_module": "voice",
                "event": "voice.tts.warm_replenish_scheduled",
                "voice_call_id": voice_call_id,
                "operation": "voice.tts.stream_segments",
                "provider": route.provider,
                "model": f"{route.provider}/{route.model}",
                "provider_voice_id": provider_voice_id,
                "audio_format": audio_profile.format,
                "warm_key": _warm_key_label(key),
                "warm_status": "scheduled",
                "source": source,
            },
        )

    async def _replenish_minimax_warm_connection(
        self,
        *,
        route: ResolvedTTSRoute,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str,
        api_key: str,
        key: _MinimaxWarmKey,
        voice_call_id: str,
        source: str,
    ) -> None:
        try:
            await self._ensure_minimax_warm_connection(
                route=route,
                audio_profile=audio_profile,
                provider_voice_id=provider_voice_id,
                api_key=api_key,
            )
        except Exception:
            logger.exception(
                "MiniMax TTS warm connection replenish failed.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_replenish_failed",
                    "voice_call_id": voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "warm_status": "failed",
                    "source": source,
                },
            )

    async def _create_minimax_warm_connection(
        self,
        *,
        key: _MinimaxWarmKey,
        route: ResolvedTTSRoute,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str,
        api_key: str,
        created_at: float,
        expires_at: float,
    ) -> _MinimaxWarmConnection:
        context_manager = self._connect(route.websocket_url, api_key=api_key)
        ws: Any | None = None
        connect_start = time.monotonic()
        try:
            ws = await context_manager.__aenter__()
            connected_at = time.monotonic()
            task_start = time.monotonic()
            await self._start_minimax_task(
                ws,
                route=route,
                audio_profile=audio_profile,
                provider_voice_id=provider_voice_id,
            )
            ready_at = time.monotonic()
            logger.info(
                "MiniMax TTS warm connection ready.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_ready",
                    "operation": "voice.tts.warm_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "connect_ms": int((connected_at - connect_start) * 1000),
                    "task_start_ms": int((ready_at - task_start) * 1000),
                    "setup_elapsed_ms": int((ready_at - created_at) * 1000),
                    "ttl_ms": int((expires_at - created_at) * 1000),
                },
            )
            return _MinimaxWarmConnection(
                key=key,
                context_manager=context_manager,
                ws=ws,
                created_at=created_at,
                ready_at=ready_at,
                expires_at=expires_at,
            )
        except asyncio.CancelledError:
            if ws is not None:
                with suppress(Exception):
                    await context_manager.__aexit__(None, None, None)
            raise
        except Exception:
            if ws is not None:
                with suppress(Exception):
                    await context_manager.__aexit__(None, None, None)
            logger.exception(
                "MiniMax TTS warm connection failed.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_failed",
                    "operation": "voice.tts.warm_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "elapsed_ms": int((time.monotonic() - created_at) * 1000),
                },
            )
            raise

    def _on_minimax_warm_task_done(
        self,
        key: _MinimaxWarmKey,
        task: asyncio.Task[_MinimaxWarmConnection],
    ) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            asyncio.create_task(
                self._remove_failed_minimax_warm_entry(key, task),
                name="voice.tts.minimax_warm_failed_cleanup",
            )

    async def _remove_failed_minimax_warm_entry(
        self,
        key: _MinimaxWarmKey,
        task: asyncio.Task[_MinimaxWarmConnection],
    ) -> None:
        async with self._minimax_warm_lock:
            entry = self._minimax_warm_connections.get(key)
            if entry is not None and entry.task is task:
                self._minimax_warm_connections.pop(key, None)

    async def _maintain_minimax_warm_entry(self, entry: _MinimaxWarmEntry) -> None:
        while True:
            now = time.monotonic()
            refresh_at = max(
                entry.created_at,
                entry.expires_at - self._minimax_warm_refresh_margin_seconds,
            )
            async with self._minimax_warm_lock:
                current = self._minimax_warm_connections.get(entry.key)
                if current is not entry:
                    return
                keepalive_until = self._minimax_warm_keepalive_until.get(entry.key, 0.0)

            if keepalive_until > now and now >= refresh_at:
                if not entry.task.done():
                    retry_budget = entry.expires_at - now
                    await asyncio.sleep(5.0 if retry_budget <= 0 else min(5.0, retry_budget))
                    continue
                refreshed = await self._refresh_minimax_warm_entry(entry)
                if refreshed:
                    return
                retry_budget = entry.expires_at - time.monotonic()
                await asyncio.sleep(5.0 if retry_budget <= 0 else min(5.0, retry_budget))
                continue

            wake_at = entry.expires_at
            if keepalive_until > now:
                wake_at = min(wake_at, refresh_at)
            delay = max(0.0, wake_at - now)
            if delay > 0:
                slept = 0.0
                while slept < delay:
                    step = min(self._minimax_warm_health_check_interval_seconds, delay - slept)
                    await asyncio.sleep(step)
                    slept += step

                    if not entry.task.done():
                        continue
                    try:
                        connection = entry.task.result()
                    except Exception:
                        return

                    if not await self._check_minimax_warm_liveness(connection):
                        async with self._minimax_warm_lock:
                            current = self._minimax_warm_connections.get(entry.key)
                            if current is entry:
                                self._minimax_warm_connections.pop(entry.key, None)
                        await self._close_minimax_warm_entry(entry)
                        logger.warning(
                            "MiniMax TTS warm connection evicted: health check failed.",
                            extra={
                                "log_module": "voice",
                                "event": "voice.tts.warm_health_check_failed",
                                "operation": "voice.tts.warm_segments",
                                "warm_key": _warm_key_label(entry.key),
                                "warm_age_ms": int(
                                    (time.monotonic() - entry.created_at) * 1000
                                ),
                                "source": "maintenance_health_check",
                            },
                        )
                        self._schedule_minimax_warm_replenishment(
                            route=entry.route,
                            audio_profile=entry.audio_profile,
                            provider_voice_id=entry.provider_voice_id,
                            api_key=entry.api_key,
                            voice_call_id="",
                            source="health_check_eviction",
                        )
                        return
                continue

            async with self._minimax_warm_lock:
                current = self._minimax_warm_connections.get(entry.key)
                if current is not entry:
                    return
                keepalive_until = self._minimax_warm_keepalive_until.get(entry.key, 0.0)
                if keepalive_until > time.monotonic():
                    continue
                self._minimax_warm_connections.pop(entry.key, None)
                self._minimax_warm_keepalive_until.pop(entry.key, None)

            await self._close_minimax_warm_entry(entry)
            logger.info(
                "MiniMax TTS warm connection expired.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_expired",
                    "operation": "voice.tts.warm_segments",
                    "warm_key": _warm_key_label(entry.key),
                    "warm_age_ms": int((time.monotonic() - entry.created_at) * 1000),
                },
            )
            return

    async def _refresh_minimax_warm_entry(self, entry: _MinimaxWarmEntry) -> bool:
        created_at = time.monotonic()
        expires_at = created_at + self._minimax_warm_ttl_seconds
        new_task = asyncio.create_task(
            self._create_minimax_warm_connection(
                key=entry.key,
                route=entry.route,
                audio_profile=entry.audio_profile,
                provider_voice_id=entry.provider_voice_id,
                api_key=entry.api_key,
                created_at=created_at,
                expires_at=expires_at,
            ),
            name="voice.tts.minimax_warm_refresh",
        )
        try:
            await asyncio.shield(new_task)
        except Exception:
            logger.exception(
                "MiniMax TTS warm connection refresh failed.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_refresh_failed",
                    "operation": "voice.tts.warm_segments",
                    "provider": entry.route.provider,
                    "model": f"{entry.route.provider}/{entry.route.model}",
                    "provider_voice_id": entry.provider_voice_id,
                    "audio_format": entry.audio_profile.format,
                    "warm_key": _warm_key_label(entry.key),
                    "warm_status": "failed",
                    "elapsed_ms": int((time.monotonic() - created_at) * 1000),
                },
            )
            return False

        new_entry = _MinimaxWarmEntry(
            key=entry.key,
            task=new_task,
            created_at=created_at,
            expires_at=expires_at,
            route=entry.route,
            audio_profile=entry.audio_profile,
            provider_voice_id=entry.provider_voice_id,
            api_key=entry.api_key,
        )
        async with self._minimax_warm_lock:
            current = self._minimax_warm_connections.get(entry.key)
            if current is not entry:
                should_keep_new = False
            else:
                self._minimax_warm_connections[entry.key] = new_entry
                should_keep_new = True

        if not should_keep_new:
            await self._close_minimax_warm_entry(new_entry)
            return True

        self._schedule_minimax_warm_maintenance(new_entry)
        await self._close_minimax_warm_entry(entry)
        logger.info(
            "MiniMax TTS warm connection refreshed.",
            extra={
                "log_module": "voice",
                "event": "voice.tts.warm_refreshed",
                "operation": "voice.tts.warm_segments",
                "provider": entry.route.provider,
                "model": f"{entry.route.provider}/{entry.route.model}",
                "provider_voice_id": entry.provider_voice_id,
                "audio_format": entry.audio_profile.format,
                "warm_key": _warm_key_label(entry.key),
                "warm_status": "refreshed",
                "warm_age_ms": int((time.monotonic() - entry.created_at) * 1000),
                "ttl_ms": int(self._minimax_warm_ttl_seconds * 1000),
            },
        )
        return True

    async def _close_minimax_warm_connection(
        self,
        connection: _MinimaxWarmConnection,
    ) -> None:
        with suppress(Exception):
            await connection.context_manager.__aexit__(None, None, None)

    async def _close_minimax_warm_entry(self, entry: _MinimaxWarmEntry) -> None:
        if entry.task.done():
            if entry.task.cancelled():
                return
            with suppress(Exception):
                connection = entry.task.result()
                await self._close_minimax_warm_connection(connection)
            return

        entry.task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await entry.task

    async def _check_minimax_warm_liveness(
        self,
        connection: _MinimaxWarmConnection,
    ) -> bool:
        try:
            await asyncio.wait_for(connection.ws.recv(), timeout=0.5)
            return True
        except TimeoutError:
            return True
        except Exception:
            return False

    async def _acquire_minimax_warm_connection(
        self,
        *,
        route: ResolvedTTSRoute,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str,
        voice_call_id: str,
    ) -> _MinimaxWarmConnection | None:
        if not self._minimax_warm_enabled:
            return None

        key = self._minimax_warm_key(
            route=route,
            audio_profile=audio_profile,
            provider_voice_id=provider_voice_id,
        )
        wait_started = time.monotonic()
        async with self._minimax_warm_lock:
            entry = self._minimax_warm_connections.pop(key, None)

        if entry is None:
            logger.info(
                "MiniMax TTS warm connection not found.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_miss",
                    "voice_call_id": voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "warm_status": "missing",
                },
            )
            return None

        if time.monotonic() >= entry.expires_at:
            await self._close_minimax_warm_entry(entry)
            logger.info(
                "MiniMax TTS warm connection expired before acquire.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_miss",
                    "voice_call_id": voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "warm_status": "expired",
                    "warm_age_ms": int((time.monotonic() - entry.created_at) * 1000),
                },
            )
            return None

        try:
            if entry.task.done():
                connection = entry.task.result()
            else:
                timeout = min(
                    self._minimax_warm_wait_seconds,
                    max(0.0, entry.expires_at - time.monotonic()),
                )
                if timeout <= 0:
                    raise TimeoutError("warm connection wait budget exhausted")
                connection = await asyncio.wait_for(asyncio.shield(entry.task), timeout=timeout)
        except TimeoutError:
            await self._close_minimax_warm_entry(entry)
            logger.info(
                "MiniMax TTS warm connection was not ready in time.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_miss",
                    "voice_call_id": voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "warm_status": "wait_timeout",
                    "warm_wait_ms": int((time.monotonic() - wait_started) * 1000),
                },
            )
            return None
        except Exception:
            logger.info(
                "MiniMax TTS warm connection failed before acquire.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_miss",
                    "voice_call_id": voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "warm_status": "failed",
                    "warm_wait_ms": int((time.monotonic() - wait_started) * 1000),
                },
            )
            return None

        if time.monotonic() >= connection.expires_at:
            await self._close_minimax_warm_connection(connection)
            logger.info(
                "MiniMax TTS warm connection expired after readiness.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_miss",
                    "voice_call_id": voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "warm_status": "expired",
                    "warm_age_ms": int((time.monotonic() - connection.created_at) * 1000),
                },
            )
            return None

        if not await self._check_minimax_warm_liveness(connection):
            await self._close_minimax_warm_connection(connection)
            logger.warning(
                "MiniMax TTS warm connection failed pre-acquire health check.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.warm_health_check_failed",
                    "voice_call_id": voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": provider_voice_id,
                    "audio_format": audio_profile.format,
                    "warm_key": _warm_key_label(key),
                    "warm_status": "dead_on_acquire",
                    "warm_age_ms": int(
                        (time.monotonic() - connection.created_at) * 1000
                    ),
                },
            )
            return None

        logger.info(
            "MiniMax TTS warm connection acquired.",
            extra={
                "log_module": "voice",
                "event": "voice.tts.warm_acquired",
                "voice_call_id": voice_call_id,
                "operation": "voice.tts.stream_segments",
                "provider": route.provider,
                "model": f"{route.provider}/{route.model}",
                "provider_voice_id": provider_voice_id,
                "audio_format": audio_profile.format,
                "warm_key": _warm_key_label(key),
                "warm_status": "acquired",
                "warm_age_ms": int((time.monotonic() - connection.created_at) * 1000),
                "warm_wait_ms": int((time.monotonic() - wait_started) * 1000),
            },
        )
        return connection

    async def _stream_minimax_text(
        self,
        *,
        route: ResolvedTTSRoute,
        text: str,
        audio_profile: TTSAudioProfile,
        provider_voice_id: str | None,
    ) -> AsyncGenerator[tuple[str, str], None]:
        normalized_text = text.strip()
        if not normalized_text:
            raise APIError(
                status_code=422,
                code="tts_text_empty",
                message="invalid request: tts text is empty",
            )

        voice_id = self._voice_id(provider_voice_id)
        api_key = self._require_api_key(route)
        input_characters = len(normalized_text)
        handle = start_voice_generation(
            VoiceCallRequest(
                operation="voice.tts.stream",
                provider=route.provider,
                model=route.model,
                input_payload={
                    "text_chars": input_characters,
                    "audio_format": audio_profile.format,
                    "provider_voice_id": voice_id,
                    "stream_mode": "single",
                },
                metadata={
                    "provider": route.provider,
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                    "mime_type": audio_profile.mime_type,
                    "sample_rate": audio_profile.sample_rate,
                    "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                    "usage_source": "input_text_characters",
                },
            )
        )
        start = time.monotonic()
        try:
            async with self._connect(route.websocket_url, api_key=api_key) as ws:
                await self._start_minimax_task(
                    ws,
                    route=route,
                    audio_profile=audio_profile,
                    provider_voice_id=voice_id,
                )
                await ws.send(_json_dumps({"event": "task_continue", "text": normalized_text}))
                await ws.send(_json_dumps({"event": "task_finish"}))
                async for audio_bytes in self._receive_minimax_audio(ws, operation="single"):
                    mark_voice_audio_delta(handle, output_bytes=len(audio_bytes))
                    yield base64.b64encode(audio_bytes).decode("ascii"), audio_profile.mime_type
            finish_voice_generation(
                handle,
                output={
                    "audio_delta_count": handle.audio_delta_count,
                    "output_bytes": handle.output_bytes,
                    "mime_type": audio_profile.mime_type,
                    "is_final": True,
                },
                usage_details=_characters_usage_details(input_characters),
                metadata={
                    "audio_format": audio_profile.format,
                    "mime_type": audio_profile.mime_type,
                    "provider_voice_id": voice_id,
                    "input_characters": input_characters,
                },
            )
            logger.info(
                "Voice TTS stream completed.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.stream_summary",
                    "voice_call_id": handle.voice_call_id,
                    "operation": "voice.tts.stream",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                },
            )
        except TimeoutError as exc:
            record_voice_generation_failure(
                handle,
                exc,
                metadata={
                    "provider": route.provider,
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                },
            )
            raise APIError(
                status_code=504,
                code="external_timeout",
                message="upstream timeout: minimax tts service response timed out",
            ) from exc
        except APIError as exc:
            record_voice_generation_failure(
                handle,
                exc,
                metadata={
                    "provider": route.provider,
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                },
            )
            raise
        except Exception as exc:
            record_voice_generation_failure(
                handle,
                exc,
                metadata={
                    "provider": route.provider,
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                },
            )
            logger.exception(
                "MiniMax TTS websocket call failed.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.request_error",
                    "voice_call_id": handle.voice_call_id,
                    "operation": "voice.tts.stream",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                },
            )
            raise APIError(
                status_code=502,
                code="tts_upstream_error",
                message=f"upstream error: minimax tts service request failed; err={exc}",
            ) from exc

    async def _stream_minimax_segments(
        self,
        *,
        route: ResolvedTTSRoute,
        segments_queue: asyncio.Queue[str | None],
        audio_profile: TTSAudioProfile,
        provider_voice_id: str | None,
        debug_capture: Any | None,
        segment_counter: list[int] | None,
    ) -> AsyncGenerator[tuple[str, str], None]:
        voice_id = self._voice_id(provider_voice_id)
        api_key = self._require_api_key(route)
        sent_segment_count = 0
        input_characters = 0
        sender_task: asyncio.Task[None] | None = None
        handle = start_voice_generation(
            VoiceCallRequest(
                operation="voice.tts.stream_segments",
                provider=route.provider,
                model=route.model,
                input_payload={
                    "audio_format": audio_profile.format,
                    "provider_voice_id": voice_id,
                    "stream_mode": "segments",
                },
                metadata={
                    "provider": route.provider,
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                    "mime_type": audio_profile.mime_type,
                    "sample_rate": audio_profile.sample_rate,
                    "billing_unit": _LANGFUSE_UNIT_CHARACTERS,
                    "usage_source": "input_text_characters",
                },
            )
        )
        start = time.monotonic()

        async def _sender(ws: Any) -> None:
            nonlocal sent_segment_count, input_characters
            while True:
                segment = await segments_queue.get()
                if segment is None:
                    break
                text = segment.strip()
                if not text:
                    continue
                sent_segment_count += 1
                input_characters += len(text)
                logger.info(
                    "TTS segment sent to MiniMax.",
                    extra={
                        "log_module": "voice",
                        "event": "voice.tts.segment_sent",
                        "voice_call_id": handle.voice_call_id,
                        "segment_idx": sent_segment_count,
                        "segment_chars": len(text),
                        "segment_text_preview": text[:50],
                        "first_segment_send_elapsed_ms": (
                            int((time.monotonic() - start) * 1000)
                            if sent_segment_count == 1
                            else None
                        ),
                        "cumulative_chars": input_characters,
                    },
                )
                if debug_capture is not None:
                    debug_capture.record_tts_segment(index=sent_segment_count, text=text)
                await ws.send(_json_dumps({"event": "task_continue", "text": text}))
            await ws.send(_json_dumps({"event": "task_finish"}))

        async def _stream_started_ws(ws: Any) -> AsyncGenerator[tuple[str, str], None]:
            nonlocal sender_task
            sender_task = asyncio.create_task(
                _sender(ws),
                name="voice.tts.minimax_segment_sender",
            )
            async for audio_bytes in self._receive_minimax_audio(
                ws,
                operation="segments",
                debug_capture=debug_capture,
            ):
                mark_voice_audio_delta(handle, output_bytes=len(audio_bytes))
                yield base64.b64encode(audio_bytes).decode("ascii"), audio_profile.mime_type
            await sender_task

        try:
            warm_connection = await self._acquire_minimax_warm_connection(
                route=route,
                audio_profile=audio_profile,
                provider_voice_id=voice_id,
                voice_call_id=handle.voice_call_id,
            )
            warm_failed = False

            if warm_connection is not None:
                self._schedule_minimax_warm_replenishment(
                    route=route,
                    audio_profile=audio_profile,
                    provider_voice_id=voice_id,
                    api_key=api_key,
                    voice_call_id=handle.voice_call_id,
                    source="after_acquire",
                )
                try:
                    async for chunk in _stream_started_ws(warm_connection.ws):
                        yield chunk
                except Exception as warm_exc:
                    warm_failed = True
                    if sender_task and not sender_task.done():
                        sender_task.cancel()
                    await self._close_minimax_warm_connection(warm_connection)
                    logger.warning(
                        "MiniMax TTS warm connection died during streaming; falling back to cold.",
                        extra={
                            "log_module": "voice",
                            "event": "voice.tts.warm_stream_fallback",
                            "voice_call_id": handle.voice_call_id,
                            "operation": "voice.tts.stream_segments",
                            "provider": route.provider,
                            "model": f"{route.provider}/{route.model}",
                            "provider_voice_id": voice_id,
                            "audio_format": audio_profile.format,
                            "warm_error": str(warm_exc),
                        },
                    )
                else:
                    await self._close_minimax_warm_connection(warm_connection)

            if warm_connection is None or warm_failed:
                self._schedule_minimax_warm_replenishment(
                    route=route,
                    audio_profile=audio_profile,
                    provider_voice_id=voice_id,
                    api_key=api_key,
                    voice_call_id=handle.voice_call_id,
                    source="after_miss" if warm_connection is None else "after_warm_failure",
                )
                connect_start = time.monotonic()
                logger.info(
                    "MiniMax segmented TTS websocket connect started.",
                    extra={
                        "log_module": "voice",
                        "event": "voice.tts.websocket_connect_start",
                        "voice_call_id": handle.voice_call_id,
                        "operation": "voice.tts.stream_segments",
                        "provider": route.provider,
                        "model": f"{route.provider}/{route.model}",
                        "provider_voice_id": voice_id,
                        "audio_format": audio_profile.format,
                    },
                )
                async with self._connect(route.websocket_url, api_key=api_key) as ws:
                    connect_end = time.monotonic()
                    logger.info(
                        "MiniMax segmented TTS websocket connected.",
                        extra={
                            "log_module": "voice",
                            "event": "voice.tts.websocket_connected",
                            "voice_call_id": handle.voice_call_id,
                            "operation": "voice.tts.stream_segments",
                            "provider": route.provider,
                            "model": f"{route.provider}/{route.model}",
                            "provider_voice_id": voice_id,
                            "audio_format": audio_profile.format,
                            "connect_ms": int((connect_end - connect_start) * 1000),
                            "setup_elapsed_ms": int((connect_end - start) * 1000),
                        },
                    )
                    task_start = time.monotonic()
                    await self._start_minimax_task(
                        ws,
                        route=route,
                        audio_profile=audio_profile,
                        provider_voice_id=voice_id,
                    )
                    task_started = time.monotonic()
                    logger.info(
                        "MiniMax segmented TTS task started.",
                        extra={
                            "log_module": "voice",
                            "event": "voice.tts.task_started",
                            "voice_call_id": handle.voice_call_id,
                            "operation": "voice.tts.stream_segments",
                            "provider": route.provider,
                            "model": f"{route.provider}/{route.model}",
                            "provider_voice_id": voice_id,
                            "audio_format": audio_profile.format,
                            "task_start_ms": int((task_started - task_start) * 1000),
                            "setup_elapsed_ms": int((task_started - start) * 1000),
                        },
                    )
                    async for chunk in _stream_started_ws(ws):
                        yield chunk
            finish_voice_generation(
                handle,
                output={
                    "audio_delta_count": handle.audio_delta_count,
                    "output_bytes": handle.output_bytes,
                    "mime_type": audio_profile.mime_type,
                    "is_final": True,
                    "segment_count": sent_segment_count,
                },
                usage_details=_characters_usage_details(input_characters),
                metadata={
                    "audio_format": audio_profile.format,
                    "mime_type": audio_profile.mime_type,
                    "provider_voice_id": voice_id,
                    "input_characters": input_characters,
                    "segment_count": sent_segment_count,
                },
            )
            logger.info(
                "Voice segmented TTS stream completed.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.stream_segments_summary",
                    "voice_call_id": handle.voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                    "elapsed_ms": int((time.monotonic() - start) * 1000),
                },
            )
        except TimeoutError as exc:
            record_voice_generation_failure(
                handle,
                exc,
                metadata={
                    "provider": route.provider,
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                    "segment_count": sent_segment_count,
                },
            )
            raise APIError(
                status_code=504,
                code="external_timeout",
                message="upstream timeout: minimax segmented tts service response timed out",
            ) from exc
        except APIError as exc:
            record_voice_generation_failure(
                handle,
                exc,
                metadata={
                    "provider": route.provider,
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                    "segment_count": sent_segment_count,
                },
            )
            raise
        except Exception as exc:
            record_voice_generation_failure(
                handle,
                exc,
                metadata={
                    "provider": route.provider,
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                    "segment_count": sent_segment_count,
                },
            )
            logger.exception(
                "MiniMax segmented TTS websocket call failed.",
                extra={
                    "log_module": "voice",
                    "event": "voice.tts.segment_request_error",
                    "voice_call_id": handle.voice_call_id,
                    "operation": "voice.tts.stream_segments",
                    "provider": route.provider,
                    "model": f"{route.provider}/{route.model}",
                    "provider_voice_id": voice_id,
                    "audio_format": audio_profile.format,
                },
            )
            raise APIError(
                status_code=502,
                code="tts_upstream_error",
                message=f"upstream error: minimax segmented tts service request failed; err={exc}",
            ) from exc
        finally:
            if segment_counter is not None:
                segment_counter[0] = sent_segment_count
            if sender_task and not sender_task.done():
                sender_task.cancel()

    async def _receive_minimax_audio(
        self,
        ws: Any,
        *,
        operation: str,
        debug_capture: Any | None = None,
    ) -> AsyncGenerator[bytes, None]:
        while True:
            raw = await asyncio.wait_for(
                ws.recv(),
                timeout=self._minimax_event_timeout_seconds,
            )
            if isinstance(raw, bytes):
                continue
            payload = _load_json(raw)
            event = _event_name(payload)
            self._raise_for_minimax_error(payload, default_message=f"{operation} synthesis failed")
            if debug_capture is not None and event:
                data = payload.get("data")
                audio_hex = data.get("audio") if isinstance(data, dict) else None
                debug_capture.record_tts_event(
                    event_type=event,
                    audio_delta_chars=len(audio_hex) if isinstance(audio_hex, str) else None,
                )

            audio_bytes = self._decode_minimax_audio_hex(payload)
            if audio_bytes:
                logger.info(
                    "TTS audio frame from MiniMax.",
                    extra={
                        "log_module": "voice",
                        "event": "voice.tts.audio_frame",
                        "frame_bytes": len(audio_bytes),
                        "frame_ms": round(len(audio_bytes) / 2 / 24, 2) if audio_bytes else 0,
                        "event_type": event,
                        "is_final": payload.get("is_final"),
                    },
                )
                yield audio_bytes

            # MiniMax marks each task_continue result with is_final when that
            # continuation is complete. The whole websocket task ends only at
            # task_finished, so do not stop receiving audio on is_final.
            if event in {"task_finished", "task_finish"}:
                break
