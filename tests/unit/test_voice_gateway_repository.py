from __future__ import annotations

import asyncio
import base64
import json
import wave
from io import BytesIO
from typing import Any

import pytest

from src.core.exceptions import APIError
from src.repositories import voice_gateway_repository as voice_gateway_module
from src.repositories.voice_gateway_repository import VoiceGatewayRepository


class _WsStub:
    def __init__(self, recv_events: list[Any]) -> None:
        self._recv_events = list(recv_events)
        self.sent_payloads: list[Any] = []

    async def send(self, payload: Any) -> None:
        self.sent_payloads.append(payload)

    async def recv(self) -> Any:
        if not self._recv_events:
            raise RuntimeError("no more events")
        return self._recv_events.pop(0)


class _ConnectCtxStub:
    def __init__(self, ws: _WsStub) -> None:
        self._ws = ws

    async def __aenter__(self) -> _WsStub:
        return self._ws

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _ConnectEnterErrorStub:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> _WsStub:
        raise self._exc

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _ObservationStub:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended = True


def _build_repo_for_tts(
    ws: _WsStub,
    *,
    captured: dict[str, str] | None = None,
) -> VoiceGatewayRepository:
    repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    repo._api_key = "sk-test"
    repo._stt_model = "fun-asr-realtime"
    repo._tts_model = "qwen3-tts-instruct-flash-realtime"
    repo._stt_event_timeout_seconds = 5.0
    repo._stt_ws_url = "wss://example.com/stt"
    repo._tts_ws_url = "wss://example.com/tts"
    repo._billing_region = "cn"

    def _connect(url: str, *, api_key: str):
        if captured is not None:
            captured["url"] = url
            captured["api_key"] = api_key
        return _ConnectCtxStub(ws)

    repo._connect = _connect
    return repo


def _build_repo_for_stt(ws: _WsStub) -> VoiceGatewayRepository:
    repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    repo._api_key = "sk-test"
    repo._stt_model = "fun-asr-realtime"
    repo._tts_model = "qwen3-tts-instruct-flash-realtime"
    repo._stt_event_timeout_seconds = 5.0
    repo._stt_ws_url = "wss://example.com/stt"
    repo._tts_ws_url = "wss://example.com/tts"
    repo._billing_region = "cn"
    repo._connect = lambda url, *, api_key: _ConnectCtxStub(ws)  # type: ignore[assignment]
    return repo


def _build_repo_for_qwen3_stt(
    ws: _WsStub,
    *,
    captured: dict[str, Any] | None = None,
) -> VoiceGatewayRepository:
    repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    repo._api_key = "sk-test"
    repo._stt_model = "qwen3-asr-flash-realtime"
    repo._tts_model = "qwen3-tts-instruct-flash-realtime"
    repo._stt_event_timeout_seconds = 5.0
    repo._stt_ws_url = "wss://example.com/stt-inference"
    repo._stt_realtime_ws_url = "wss://example.com/stt-realtime"
    repo._tts_ws_url = "wss://example.com/tts"
    repo._billing_region = "cn"

    def _connect(url: str, *, api_key: str, extra_headers: dict[str, str] | None = None):
        if captured is not None:
            captured["url"] = url
            captured["api_key"] = api_key
            captured["extra_headers"] = extra_headers
        return _ConnectCtxStub(ws)

    repo._connect = _connect
    return repo


def _build_repo_for_clone() -> VoiceGatewayRepository:
    repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    repo._api_key = "sk-test"
    repo._voice_enrollment_model = "voice-enrollment"
    repo._voice_clone_target_model = "cosyvoice-v3-flash"
    repo._tts_customization_api_url = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
    repo._http_timeout_seconds = 30.0
    repo._timbre_list_doc_url = "https://help.aliyun.com/zh/model-studio/multimodal-timbre-list"
    repo._timbre_list_cache_seconds = 600
    repo._billing_region = "cn"
    repo._system_voices_cache = None
    repo._system_voices_cache_expire_at = 0.0
    return repo


def _build_wav_16k_mono_bytes() -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 3200)
    return buf.getvalue()




@pytest.mark.asyncio
async def test_cosyvoice_tts_uses_inference_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = b"\xff\xfb\x90\x00"
    ws = _WsStub(
        recv_events=[
            json.dumps({"header": {"event": "task-started"}}),
            frame,
            json.dumps({"header": {"event": "result-generated"}, "payload": {}}),
            json.dumps({"header": {"event": "task-finished"}}),
        ]
    )
    observation = _ObservationStub()
    monkeypatch.setattr(
        voice_gateway_module,
        "_start_voice_model_observation",
        lambda **kwargs: observation,
    )
    captured: dict[str, str] = {}
    repo = _build_repo_for_tts(ws, captured=captured)
    repo._tts_inference_ws_url = "wss://example.com/inference"

    chunks = []
    async for chunk in repo.stream_tts_audio_base64(
        text="Hello there.",
        audio_format="mp3",
        provider_voice_id="cosyvoice-v3-flash-demo",
        provider_model="cosyvoice-v3-flash",
    ):
        chunks.append(chunk)

    assert captured["url"] == "wss://example.com/inference"
    assert chunks == [
        (base64.b64encode(frame).decode("ascii"), "audio/mpeg"),
    ]
    run_task = json.loads(ws.sent_payloads[0])
    assert run_task["header"]["action"] == "run-task"
    assert run_task["payload"]["model"] == "cosyvoice-v3-flash"
    assert run_task["payload"]["parameters"]["voice"] == "cosyvoice-v3-flash-demo"
    assert run_task["payload"]["parameters"]["text_type"] == "PlainText"
    actions = [
        json.loads(payload)["header"]["action"] for payload in ws.sent_payloads[1:]
    ]
    assert actions == ["continue-task", "finish-task"]
    assert observation.ended is True


@pytest.mark.asyncio
async def test_tts_records_langfuse_character_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.updated"}),
            json.dumps({"type": "response.audio.delta", "delta": "AQI="}),
            json.dumps({"type": "response.done"}),
        ]
    )
    repo = _build_repo_for_tts(ws)
    observation = _ObservationStub()
    captured_start: dict[str, Any] = {}

    def _start_observation_stub(**kwargs: Any) -> _ObservationStub:
        captured_start.update(kwargs)
        return observation

    monkeypatch.setattr(
        voice_gateway_module,
        "_start_voice_model_observation",
        _start_observation_stub,
    )

    items = []
    async for item in repo.stream_tts_audio_base64(
        text="你好hello",
        audio_format="mp3",
        provider_voice_id="Serena",
        provider_model="qwen3-tts-vc-realtime-2026-01-15",
    ):
        items.append(item)

    assert items == [("AQI=", "audio/mpeg")]
    assert captured_start["name"] == "voice.tts.stream"
    assert captured_start["model"] == "qwen3-tts-vc-realtime-2026-01-15"
    assert observation.ended is True
    assert observation.updates[-1]["usage_details"] == {
        "input": len("你好hello"),
        "total": len("你好hello"),
        "unit": "CHARACTERS",
    }
    assert observation.updates[-1]["metadata"]["usage_source"] == "input_text_characters"
    assert observation.updates[-1]["metadata"]["provider_voice_id"] == "Serena"




@pytest.mark.asyncio
async def test_stt_raises_upstream_error_with_header_error_message() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps(
                {
                    "header": {
                        "event": "task-failed",
                        "error_message": "Missing required parameter 'payload.input'!",
                    }
                }
            )
        ]
    )
    repo = _build_repo_for_stt(ws)

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio(
            audio_bytes=b"\x01" * 3200,
            audio_format="wav",
            sample_rate=16000,
        )

    assert exc.value.status_code == 502
    assert exc.value.code == "stt_upstream_error"
    assert "payload.input" not in exc.value.message




@pytest.mark.asyncio
async def test_qwen3_stt_retries_connection_reset_during_websocket_open() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好"}),
            json.dumps({"type": "session.finished", "request_id": "req-stt-retry"}),
        ]
    )
    repo = _build_repo_for_qwen3_stt(ws)
    repo._stt_websocket_retry_delay_seconds = 0.0
    attempts: list[dict[str, Any]] = []

    def _connect(url: str, *, api_key: str, extra_headers: dict[str, str] | None = None):
        attempts.append(
            {
                "url": url,
                "api_key": api_key,
                "extra_headers": extra_headers,
            }
        )
        if len(attempts) == 1:
            return _ConnectEnterErrorStub(
                ConnectionResetError(10054, "remote host forcibly closed the connection")
            )
        return _ConnectCtxStub(ws)

    repo._connect = _connect

    text, request_id = await repo.transcribe_audio(
        audio_bytes=_build_wav_16k_mono_bytes(),
        audio_format="wav",
        sample_rate=16000,
    )

    assert text == "你好"
    assert request_id == "req-stt-retry"
    assert len(attempts) == 2
    assert {attempt["url"] for attempt in attempts} == {
        "wss://example.com/stt-realtime?model=qwen3-asr-flash-realtime"
    }
    assert all(attempt["api_key"] == "sk-test" for attempt in attempts)
    assert all(
        attempt["extra_headers"] == {"OpenAI-Beta": "realtime=v1"}
        for attempt in attempts
    )




@pytest.mark.asyncio
async def test_tts_stream_from_segments_reuses_single_session() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.updated"}),
            json.dumps({"type": "response.audio.delta", "delta": "AQI="}),
            json.dumps({"type": "session.finished"}),
        ]
    )
    repo = _build_repo_for_tts(ws)

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    await queue.put("Hello,")
    await queue.put(" world.")
    await queue.put(None)

    items = []
    async for item in repo.stream_tts_audio_base64_from_segments(
        segments_queue=queue,
        audio_format="pcm",
    ):
        items.append(item)

    assert items == [("AQI=", "audio/pcm;rate=24000")]
    sent_types = [json.loads(payload)["type"] for payload in ws.sent_payloads]
    assert sent_types == [
        "session.update",
        "input_text_buffer.append",
        "input_text_buffer.append",
        "session.finish",
    ]




@pytest.mark.asyncio
async def test_tts_stream_from_segments_keeps_receiving_after_response_done() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.updated"}),
            json.dumps({"type": "response.audio.delta", "delta": "AQI="}),
            json.dumps({"type": "response.audio.done"}),
            json.dumps({"type": "response.done"}),
            json.dumps({"type": "response.audio.delta", "delta": "AwQ="}),
            json.dumps({"type": "session.finished"}),
        ]
    )
    repo = _build_repo_for_tts(ws)

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    await queue.put("Hello,")
    await queue.put(" world.")
    await queue.put(None)

    items = []
    async for item in repo.stream_tts_audio_base64_from_segments(
        segments_queue=queue,
        audio_format="pcm",
    ):
        items.append(item)

    assert items == [
        ("AQI=", "audio/pcm;rate=24000"),
        ("AwQ=", "audio/pcm;rate=24000"),
    ]


@pytest.mark.asyncio
async def test_create_voice_clone_calls_dashscope_customization_endpoint() -> None:
    repo = _build_repo_for_clone()
    captured_payload: dict[str, Any] = {}
    observation = _ObservationStub()
    captured_start: dict[str, Any] = {}

    async def _post_stub(payload: dict[str, Any]) -> dict[str, Any]:
        captured_payload.update(payload)
        return {
            "request_id": "req-create-1",
            "output": {
                "voice_id": "bob_new",
                "target_model": "cosyvoice-v3-flash",
            },
        }

    repo._post_tts_customization = _post_stub  # type: ignore[method-assign]

    def _start_observation_stub(**kwargs: Any) -> _ObservationStub:
        captured_start.update(kwargs)
        return observation

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        voice_gateway_module,
        "_start_voice_model_observation",
        _start_observation_stub,
    )
    try:
        result = await repo.create_voice_clone(
            provider="dashscope",
            display_name="My Voice",
            source_audio_bytes=_build_wav_16k_mono_bytes(),
            source_audio_format="wav",
            description=None,
            language_hint="zh",
            idempotency_key=None,
        )
    finally:
        monkeypatch.undo()

    assert captured_start["name"] == "voice.clone.create"
    assert captured_start["model"] == "voice-enrollment"
    assert captured_start["input_payload"]["target_model"] == "cosyvoice-v3-flash"
    assert observation.ended is True
    assert observation.updates[-1]["usage_details"] == {
        "input": 1,
        "total": 1,
    }
    assert observation.updates[-1]["metadata"]["billing_unit"] == "SECONDS"
    assert observation.updates[-1]["metadata"]["target_model"] == (
        "dashscope/cosyvoice-v3-flash"
    )
    assert captured_payload["model"] == "voice-enrollment"
    assert captured_payload["input"]["action"] == "create_voice"
    assert captured_payload["input"]["target_model"] == "cosyvoice-v3-flash"
    assert captured_payload["input"]["audio"].startswith("data:audio/wav;base64,")
    assert captured_payload["input"]["language_hints"] == ["zh"]
    assert captured_payload["input"]["enable_preprocess"] is True
    assert captured_payload["input"]["enable_volume_normalization"] == "true"
    assert captured_payload["input"]["max_prompt_audio_length"] > 0
    assert captured_payload["input"]["prefix"].startswith("p")
    assert result["provider_voice_id"] == "bob_new"
    assert result["provider_status"] == "active"
    assert result["provider_model"] == "cosyvoice-v3-flash"
    assert result["provider_request_id"] == "req-create-1"






