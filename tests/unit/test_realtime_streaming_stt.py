"""Unit tests for the progressive realtime STT session."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from src.realtime.services.streaming_stt import StreamingSTTError, StreamingSTTSession


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if item is None:  # type: ignore[comparison-overlap]
            raise RuntimeError("socket closed")
        return item

    async def close(self) -> None:
        self.closed = True

    def push(self, payload: dict) -> None:
        self.incoming.put_nowait(json.dumps(payload))


class _FakeConnect:
    def __init__(self, ws: _FakeWebSocket) -> None:
        self.ws = ws

    async def __aenter__(self) -> _FakeWebSocket:
        return self.ws

    async def __aexit__(self, *args) -> None:
        await self.ws.close()


def _factory(ws: _FakeWebSocket):
    def connect(*args, **kwargs):
        return _FakeConnect(ws)

    return connect


async def _started_session(ws: _FakeWebSocket) -> StreamingSTTSession:
    session = StreamingSTTSession(
        ws_url="ws://stt.test/realtime?model=qwen3-asr-flash-realtime",
        api_key="test",
        sample_rate=16000,
        connect_factory=_factory(ws),
        timeout_seconds=2,
    )
    task = asyncio.create_task(session.start())
    await asyncio.sleep(0)
    ws.push({"type": "session.updated", "session": {}})
    await task
    return session


@pytest.mark.asyncio
async def test_streams_pcm_and_returns_final_transcript() -> None:
    ws = _FakeWebSocket()
    session = await _started_session(ws)

    session.append(b"\x01\x00" * 160)
    session.append(b"\x02\x00" * 160)
    ws.push(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "delta": "你好",
        }
    )
    finish_task = asyncio.create_task(session.finish())
    await asyncio.sleep(0.05)
    ws.push(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "你好，世界",
        }
    )
    ws.push({"type": "session.finished"})

    text = await finish_task

    assert text == "你好，世界"
    append_events = [event for event in ws.sent if event["type"] == "input_audio_buffer.append"]
    assert len(append_events) == 2
    assert base64.b64decode(append_events[0]["audio"]) == b"\x01\x00" * 160
    assert [event["type"] for event in ws.sent][-2:] == [
        "input_audio_buffer.commit",
        "session.finish",
    ]
    assert session.appended_audio_ms == 20


@pytest.mark.asyncio
async def test_upstream_error_surfaces_as_streaming_error() -> None:
    ws = _FakeWebSocket()
    session = await _started_session(ws)
    finish_task = asyncio.create_task(session.finish())
    await asyncio.sleep(0.05)
    ws.push({"type": "error", "message": "quota exceeded"})

    with pytest.raises(StreamingSTTError):
        await finish_task


