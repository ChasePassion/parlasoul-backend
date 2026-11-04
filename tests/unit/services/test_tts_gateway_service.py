from __future__ import annotations

import asyncio
import base64
import json

import pytest

from src.core.exceptions import APIError
from src.core.tts_registry import TTSRegistry, TTSRouter
from src.core.tts_settings import TTSSettings
from src.services.tts_gateway_service import TTSGatewayService


class _FakeMinimaxWebSocket:
    def __init__(
        self,
        recv_payloads: list[dict[str, object]],
        *,
        recv_error: Exception | None = None,
    ) -> None:
        self._recv_payloads = [json.dumps(item) for item in recv_payloads]
        self._recv_error = recv_error
        self.sent: list[dict[str, object]] = []
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, *args):
        self.exit_count += 1
        return None

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._recv_payloads:
            if self._recv_error is not None:
                raise self._recv_error
            raise TimeoutError("no more messages")
        return self._recv_payloads.pop(0)


class _FakeMinimaxConnector:
    def __init__(self, *websockets: _FakeMinimaxWebSocket) -> None:
        self._websockets = list(websockets)
        self.calls = 0

    def __call__(self, *args, **kwargs) -> _FakeMinimaxWebSocket:
        self.calls += 1
        if not self._websockets:
            raise AssertionError("unexpected MiniMax websocket connection")
        return self._websockets.pop(0)


def _service(*websockets: _FakeMinimaxWebSocket) -> TTSGatewayService:
    registry = TTSRegistry(
        TTSSettings(
            MINIMAX_TTS_API_KEY="test-key",
            _env_file=None,
        )
    )
    # These tests exercise the MiniMax transport directly; production routes
    # legacy MiniMax system voices through DashScope (see the migration test).
    registry._router = TTSRouter(system_provider="minimax", clone_provider="dashscope")
    service = TTSGatewayService(registry=registry)
    service._connect = _FakeMinimaxConnector(*websockets)  # type: ignore[method-assign]
    return service


@pytest.mark.asyncio
async def test_minimax_single_text_stream_decodes_hex_audio() -> None:
    ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
            {"event": "task_continued", "data": {"audio": "0102"}, "is_final": False},
            {"event": "task_continued", "data": {"audio": "0304"}, "is_final": True},
            {"event": "task_finished"},
        ]
    )

    chunks = []
    async for audio_b64, mime_type in _service(ws).stream_tts_audio_base64(
        text="hello",
        audio_format="mp3",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    ):
        chunks.append((audio_b64, mime_type))

    assert [base64.b64decode(item[0]) for item in chunks] == [b"\x01\x02", b"\x03\x04"]
    assert {item[1] for item in chunks} == {"audio/mpeg"}
    assert ws.sent[0]["event"] == "task_start"
    assert ws.sent[0]["voice_setting"]["voice_id"] == "Chinese (Mandarin)_Wise_Women"
    assert ws.sent[1] == {"event": "task_continue", "text": "hello"}
    assert ws.sent[2] == {"event": "task_finish"}


@pytest.mark.asyncio
async def test_minimax_segment_stream_keeps_receiving_after_continue_is_final() -> None:
    ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
            {"event": "task_continued", "data": {"audio": "0102"}, "is_final": True},
            {"event": "task_continued", "data": {"audio": "0304"}, "is_final": True},
            {"event": "task_finished"},
        ]
    )
    segments: asyncio.Queue[str | None] = asyncio.Queue()
    await segments.put("first segment")
    await segments.put("second segment")
    await segments.put(None)

    chunks = []
    async for audio_b64, mime_type in _service(ws).stream_tts_audio_base64_from_segments(
        segments_queue=segments,
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    ):
        chunks.append((audio_b64, mime_type))

    assert [base64.b64decode(item[0]) for item in chunks] == [b"\x01\x02", b"\x03\x04"]
    assert {item[1] for item in chunks} == {"audio/pcm;rate=24000"}
    assert [item["event"] for item in ws.sent] == [
        "task_start",
        "task_continue",
        "task_continue",
        "task_finish",
    ]


@pytest.mark.asyncio
async def test_minimax_segment_stream_reuses_warmed_connection() -> None:
    ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
            # consumed by pre-acquire liveness probe:
            {"event": "__liveness_probe__"},
            {"event": "task_continued", "data": {"audio": "0102"}, "is_final": True},
            {"event": "task_finished"},
        ]
    )
    refill_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
        ]
    )
    service = _service(ws, refill_ws)

    warmed = await service.warm_tts_audio_base64_from_segments(
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    )

    segments: asyncio.Queue[str | None] = asyncio.Queue()
    await segments.put("warmed segment")
    await segments.put(None)

    chunks = []
    async for audio_b64, mime_type in service.stream_tts_audio_base64_from_segments(
        segments_queue=segments,
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    ):
        chunks.append((audio_b64, mime_type))

    assert warmed is True
    assert [base64.b64decode(item[0]) for item in chunks] == [b"\x01\x02"]
    assert {item[1] for item in chunks} == {"audio/pcm;rate=24000"}
    assert [item["event"] for item in ws.sent] == [
        "task_start",
        "task_continue",
        "task_finish",
    ]
    assert ws.enter_count == 1
    assert ws.exit_count == 1


@pytest.mark.asyncio
async def test_minimax_segment_stream_replenishes_after_warmed_connection_is_acquired() -> None:
    first_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
            # consumed by pre-acquire liveness probe:
            {"event": "__liveness_probe__"},
            {"event": "task_continued", "data": {"audio": "0102"}, "is_final": True},
            {"event": "task_finished"},
        ]
    )
    refill_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
        ]
    )
    service = _service(first_ws, refill_ws)

    warmed = await service.warm_tts_audio_base64_from_segments(
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    )

    segments: asyncio.Queue[str | None] = asyncio.Queue()
    await segments.put("first warmed segment")
    await segments.put(None)

    chunks = []
    async for audio_b64, mime_type in service.stream_tts_audio_base64_from_segments(
        segments_queue=segments,
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    ):
        chunks.append((audio_b64, mime_type))

    for _ in range(20):
        if refill_ws.enter_count:
            break
        await asyncio.sleep(0)

    assert warmed is True
    assert [base64.b64decode(item[0]) for item in chunks] == [b"\x01\x02"]
    assert first_ws.exit_count == 1
    assert refill_ws.enter_count == 1
    assert refill_ws.sent[0]["event"] == "task_start"


@pytest.mark.asyncio
async def test_minimax_warm_connection_refreshes_while_keepalive_is_active() -> None:
    first_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
        ]
    )
    refresh_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
        ]
    )
    service = _service(first_ws, refresh_ws)
    service._minimax_warm_ttl_seconds = 0.2
    service._minimax_warm_refresh_margin_seconds = 0.05
    service._minimax_warm_keepalive_seconds = 0.5

    warmed = await service.warm_tts_audio_base64_from_segments(
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    )

    for _ in range(30):
        if refresh_ws.sent:
            break
        await asyncio.sleep(0.01)

    async with service._minimax_warm_lock:
        entries = list(service._minimax_warm_connections.values())
        service._minimax_warm_connections.clear()
        service._minimax_warm_keepalive_until.clear()
    for entry in entries:
        await service._close_minimax_warm_entry(entry)

    assert warmed is True
    assert first_ws.exit_count == 1
    assert refresh_ws.enter_count == 1
    assert refresh_ws.sent[0]["event"] == "task_start"


@pytest.mark.asyncio
async def test_minimax_rejects_unsupported_opus_before_connect() -> None:
    ws = _FakeMinimaxWebSocket([])

    with pytest.raises(APIError) as exc:
        async for _ in _service(ws).stream_tts_audio_base64(
            text="hello",
            audio_format="opus",
            provider="minimax",
            provider_voice_id="Chinese (Mandarin)_Wise_Women",
            provider_model=None,
            voice_source_type="system",
        ):
            pass

    assert exc.value.code == "validation_failed"
    assert ws.sent == []


class _FakeConnectionClosedError(Exception):
    pass


@pytest.mark.asyncio
async def test_check_minimax_warm_liveness_returns_false_for_dead_ws() -> None:
    dead_ws = _FakeMinimaxWebSocket(
        [],
        recv_error=_FakeConnectionClosedError("connection closed"),
    )
    service = _service()
    from src.services.tts_gateway_service import _MinimaxWarmConnection, _MinimaxWarmKey
    import time

    connection = _MinimaxWarmConnection(
        key=_MinimaxWarmKey(
            websocket_url="wss://example.com",
            provider="minimax",
            model="speech-2.8-turbo",
            provider_voice_id="voice-1",
            audio_format="pcm",
            sample_rate=24000,
            channels=1,
            bitrate=128000,
        ),
        context_manager=None,
        ws=dead_ws,
        created_at=time.monotonic(),
        ready_at=time.monotonic(),
        expires_at=time.monotonic() + 100,
    )
    assert await service._check_minimax_warm_liveness(connection) is False


@pytest.mark.asyncio
async def test_check_minimax_warm_liveness_returns_true_for_healthy_ws() -> None:
    healthy_ws = _FakeMinimaxWebSocket([])
    service = _service()
    from src.services.tts_gateway_service import _MinimaxWarmConnection, _MinimaxWarmKey
    import time

    connection = _MinimaxWarmConnection(
        key=_MinimaxWarmKey(
            websocket_url="wss://example.com",
            provider="minimax",
            model="speech-2.8-turbo",
            provider_voice_id="voice-1",
            audio_format="pcm",
            sample_rate=24000,
            channels=1,
            bitrate=128000,
        ),
        context_manager=None,
        ws=healthy_ws,
        created_at=time.monotonic(),
        ready_at=time.monotonic(),
        expires_at=time.monotonic() + 100,
    )
    assert await service._check_minimax_warm_liveness(connection) is True


@pytest.mark.asyncio
async def test_acquire_returns_none_when_pre_acquire_health_check_fails() -> None:
    dead_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
        ],
        recv_error=_FakeConnectionClosedError("connection closed"),
    )
    refill_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
        ],
    )
    service = _service(dead_ws, refill_ws)

    warmed = await service.warm_tts_audio_base64_from_segments(
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    )

    assert warmed is True

    route = service._registry.resolve_explicit_route(
        provider="minimax",
        model=None,
        source_type="system",
    )
    audio_profile = service._registry.resolve_audio_profile(
        provider=route.provider,
        model=route.model,
        audio_format="pcm",
        stream_required=True,
    )

    result = await service._acquire_minimax_warm_connection(
        route=route,
        audio_profile=audio_profile,
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        voice_call_id="test-call",
    )
    assert result is None


@pytest.mark.asyncio
async def test_segment_stream_falls_back_to_cold_when_warm_dies() -> None:
    warm_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
        ],
        recv_error=_FakeConnectionClosedError("connection closed"),
    )
    cold_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
            {"event": "task_continued", "data": {"audio": "0102"}, "is_final": True},
            {"event": "task_finished"},
        ],
    )
    refill_ws = _FakeMinimaxWebSocket(
        [
            {"event": "connected_success"},
            {"event": "task_started"},
        ],
    )
    service = _service(warm_ws, cold_ws, refill_ws)

    warmed = await service.warm_tts_audio_base64_from_segments(
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    )
    assert warmed is True

    segments: asyncio.Queue[str | None] = asyncio.Queue()
    await segments.put("hello")
    await segments.put(None)

    chunks: list[tuple[str, str]] = []
    async for audio_b64, mime_type in service.stream_tts_audio_base64_from_segments(
        segments_queue=segments,
        audio_format="pcm",
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model=None,
        voice_source_type="system",
    ):
        chunks.append((audio_b64, mime_type))

    assert len(chunks) == 1
    assert base64.b64decode(chunks[0][0]) == b"\x01\x02"


def test_legacy_minimax_system_voice_maps_to_dashscope() -> None:
    registry = TTSRegistry(
        TTSSettings(
            MINIMAX_TTS_API_KEY="test-key",
            _env_file=None,
        )
    )
    service = TTSGatewayService(registry=registry)

    provider, model, voice = service._normalize_system_voice_inputs(
        provider="minimax",
        provider_model="speech-2.8-turbo",
        provider_voice_id="Arnold",
        voice_source_type="system",
    )

    assert provider == "dashscope"
    assert model is None
    assert voice == "Vincent"


def test_legacy_minimax_unknown_voice_falls_back_to_default_dashscope_voice() -> None:
    registry = TTSRegistry(
        TTSSettings(
            MINIMAX_TTS_API_KEY="test-key",
            _env_file=None,
        )
    )
    service = TTSGatewayService(registry=registry)

    provider, model, voice = service._normalize_system_voice_inputs(
        provider="minimax",
        provider_model="speech-2.8-turbo",
        provider_voice_id="not-in-catalog",
        voice_source_type="system",
    )

    assert provider == "dashscope"
    assert model is None
    assert voice == "Cherry"
