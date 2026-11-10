from __future__ import annotations

import asyncio
import base64
import json

import pytest

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






class _FakeConnectionClosedError(Exception):
    pass




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


