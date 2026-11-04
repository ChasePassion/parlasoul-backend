from __future__ import annotations

import asyncio
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
    repo._voice_enrollment_model = "qwen-voice-enrollment"
    repo._voice_clone_target_model = "qwen3-tts-vc-realtime-2026-01-15"
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
async def test_tts_waits_for_session_updated_before_append() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "session.updated"}),
            json.dumps({"type": "response.audio.delta", "delta": "AQI="}),
            json.dumps({"type": "response.done"}),
        ]
    )
    repo = _build_repo_for_tts(ws)

    items = []
    async for item in repo.stream_tts_audio_base64(text="hello", audio_format="mp3"):
        items.append(item)

    assert items == [("AQI=", "audio/mpeg")]
    sent_types = [json.loads(payload)["type"] for payload in ws.sent_payloads]
    assert sent_types == [
        "session.update",
        "input_text_buffer.append",
        "session.finish",
    ]
    session_update_payload = json.loads(ws.sent_payloads[0])
    assert session_update_payload["session"]["voice"] == "Cherry"
    assert session_update_payload["session"]["mode"] == "server_commit"
    assert session_update_payload["session"]["response_format"] == "mp3"
    assert "event_id" in session_update_payload


@pytest.mark.asyncio
async def test_tts_connect_uses_model_query_param() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.updated"}),
            json.dumps({"type": "response.done"}),
        ]
    )
    captured: dict[str, str] = {}
    repo = _build_repo_for_tts(ws, captured=captured)

    async for _ in repo.stream_tts_audio_base64(text="hello", audio_format="mp3"):
        pass

    assert captured["api_key"] == "sk-test"
    assert captured["url"] == "wss://example.com/tts?model=qwen3-tts-instruct-flash-realtime"


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
async def test_tts_raises_when_session_update_returns_error() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "error", "message": "Model not found (qwen-omni...)!"}),
        ]
    )
    repo = _build_repo_for_tts(ws)

    with pytest.raises(APIError) as exc:
        async for _ in repo.stream_tts_audio_base64(text="hello", audio_format="mp3"):
            pass

    assert exc.value.status_code == 502
    assert exc.value.code == "tts_upstream_error"


@pytest.mark.asyncio
async def test_stt_run_and_finish_payload_match_dashscope_protocol() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"header": {"event": "task-started"}}),
            json.dumps(
                {
                    "header": {"event": "result-generated"},
                    "payload": {"output": {"sentence": {"text": "hello"}}},
                }
            ),
            json.dumps({"header": {"event": "task-finished"}, "payload": {"output": {}}}),
        ]
    )
    repo = _build_repo_for_stt(ws)

    text, _ = await repo.transcribe_audio(
        audio_bytes=b"\x01" * 3200,
        audio_format="wav",
        sample_rate=16000,
    )

    assert text == "hello"

    run_task_payload = json.loads(ws.sent_payloads[0])
    assert run_task_payload["header"]["action"] == "run-task"
    assert "task_id" in run_task_payload["header"]
    assert run_task_payload["payload"]["input"] == {}
    assert run_task_payload["payload"]["parameters"]["format"] == "wav"
    assert run_task_payload["payload"]["parameters"]["sample_rate"] == 16000

    finish_task_payload = json.loads(ws.sent_payloads[-1])
    assert finish_task_payload["header"]["action"] == "finish-task"
    assert "task_id" in finish_task_payload["header"]
    assert finish_task_payload["payload"]["input"] == {}


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
    assert "payload.input" in exc.value.message


@pytest.mark.asyncio
async def test_qwen3_stt_uses_realtime_protocol_and_extracts_transcript() -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好"}),
            json.dumps({"type": "session.finished"}),
        ]
    )
    captured: dict[str, Any] = {}
    repo = _build_repo_for_qwen3_stt(ws, captured=captured)

    text, _ = await repo.transcribe_audio(
        audio_bytes=_build_wav_16k_mono_bytes(),
        audio_format="wav",
        sample_rate=16000,
    )

    assert text == "你好"
    assert captured["url"] == "wss://example.com/stt-realtime?model=qwen3-asr-flash-realtime"
    assert captured["extra_headers"] == {"OpenAI-Beta": "realtime=v1"}

    sent_payloads = [json.loads(p) for p in ws.sent_payloads if isinstance(p, str)]
    sent_types = [payload["type"] for payload in sent_payloads]
    assert sent_types[0] == "session.update"
    assert "input_audio_buffer.append" in sent_types
    assert "input_audio_buffer.commit" in sent_types
    assert sent_types[-1] == "session.finish"


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
async def test_qwen3_stt_records_langfuse_seconds_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好"}),
            json.dumps({"type": "session.finished", "request_id": "req-stt-1"}),
        ]
    )
    repo = _build_repo_for_qwen3_stt(ws)
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

    text, request_id = await repo.transcribe_audio(
        audio_bytes=_build_wav_16k_mono_bytes(),
        audio_format="wav",
        sample_rate=16000,
    )

    assert text == "你好"
    assert request_id == "req-stt-1"
    assert captured_start["name"] == "voice.stt.transcribe"
    assert captured_start["model"] == "qwen3-asr-flash-realtime"
    assert observation.ended is True
    assert observation.updates[-1]["usage_details"] == {
        "input": 1,
        "total": 1,
    }
    assert observation.updates[-1]["metadata"]["usage_source"] == "wav_header"
    assert observation.updates[-1]["metadata"]["source_unit"] == "SECONDS"
    assert observation.updates[-1]["metadata"]["billing_unit"] == "SECONDS"
    assert observation.updates[-1]["metadata"]["audio_duration_seconds"] == 0.2


@pytest.mark.asyncio
async def test_qwen3_stt_rejects_wav_with_wrong_sample_rate() -> None:
    ws = _WsStub(recv_events=[])
    repo = _build_repo_for_qwen3_stt(ws)
    wav_bytes = _build_wav_16k_mono_bytes()

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio(
            audio_bytes=wav_bytes,
            audio_format="wav",
            sample_rate=8000,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "stt_invalid_audio"


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
async def test_tts_stream_from_segments_records_accumulated_character_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _WsStub(
        recv_events=[
            json.dumps({"type": "session.updated"}),
            json.dumps({"type": "response.audio.delta", "delta": "AQI="}),
            json.dumps({"type": "session.finished"}),
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

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    await queue.put(" 你好 ")
    await queue.put(" world ")
    await queue.put(None)

    items = []
    async for item in repo.stream_tts_audio_base64_from_segments(
        segments_queue=queue,
        audio_format="pcm",
        provider_voice_id="Cherry",
    ):
        items.append(item)

    assert items == [("AQI=", "audio/pcm;rate=24000")]
    assert captured_start["name"] == "voice.tts.stream_segments"
    assert observation.ended is True
    assert observation.updates[-1]["usage_details"] == {
        "input": len("你好") + len("world"),
        "total": len("你好") + len("world"),
        "unit": "CHARACTERS",
    }
    assert observation.updates[-1]["metadata"]["segment_count"] == 2


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
                "voice": "bob_new",
                "target_model": "qwen3-tts-vc-realtime-2026-01-15",
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
    assert captured_start["model"] == "qwen-voice-enrollment"
    assert captured_start["input_payload"]["target_model"] == "qwen3-tts-vc-realtime-2026-01-15"
    assert observation.ended is True
    assert observation.updates[-1]["usage_details"] == {
        "input": 1,
        "total": 1,
    }
    assert observation.updates[-1]["metadata"]["billing_unit"] == "SECONDS"
    assert observation.updates[-1]["metadata"]["target_model"] == (
        "dashscope/qwen3-tts-vc-realtime-2026-01-15"
    )
    assert captured_payload["model"] == "qwen-voice-enrollment"
    assert captured_payload["input"]["action"] == "create"
    assert captured_payload["input"]["target_model"] == "qwen3-tts-vc-realtime-2026-01-15"
    assert captured_payload["input"]["audio"]["data"].startswith("data:audio/wav;base64,")
    assert captured_payload["input"]["language"] == "zh"
    assert result["provider_voice_id"] == "bob_new"
    assert result["provider_status"] == "active"
    assert result["provider_model"] == "qwen3-tts-vc-realtime-2026-01-15"
    assert result["provider_request_id"] == "req-create-1"


@pytest.mark.asyncio
async def test_create_voice_clone_legacy_payload_contract() -> None:
    repo = _build_repo_for_clone()
    captured_payload: dict[str, Any] = {}

    async def _post_stub(payload: dict[str, Any]) -> dict[str, Any]:
        captured_payload.update(payload)
        return {
            "request_id": "req-create-1",
            "output": {
                "voice": "bob_new",
                "target_model": "qwen3-tts-vc-realtime-2026-01-15",
            },
        }

    repo._post_tts_customization = _post_stub  # type: ignore[method-assign]

    result = await repo.create_voice_clone(
        provider="dashscope",
        display_name="My Voice",
        source_audio_bytes=b"abc",
        source_audio_format="wav",
        description=None,
        language_hint="zh",
        idempotency_key=None,
    )

    assert captured_payload["model"] == "qwen-voice-enrollment"
    assert captured_payload["input"]["action"] == "create"
    assert captured_payload["input"]["target_model"] == "qwen3-tts-vc-realtime-2026-01-15"
    assert captured_payload["input"]["audio"]["data"].startswith("data:audio/wav;base64,")
    assert captured_payload["input"]["language"] == "zh"
    assert result["provider_voice_id"] == "bob_new"
    assert result["provider_status"] == "active"
    assert result["provider_model"] == "qwen3-tts-vc-realtime-2026-01-15"
    assert result["provider_request_id"] == "req-create-1"


@pytest.mark.asyncio
async def test_create_voice_clone_rejects_unsupported_audio_format() -> None:
    repo = _build_repo_for_clone()

    with pytest.raises(APIError) as exc:
        await repo.create_voice_clone(
            provider="dashscope",
            display_name="My Voice",
            source_audio_bytes=b"abc",
            source_audio_format="aac",
            description=None,
            language_hint=None,
            idempotency_key=None,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "validation_failed"


@pytest.mark.asyncio
async def test_delete_voice_clone_calls_dashscope_customization_endpoint() -> None:
    repo = _build_repo_for_clone()
    captured_payload: dict[str, Any] = {}
    observation = _ObservationStub()
    captured_start: dict[str, Any] = {}

    async def _post_stub(payload: dict[str, Any]) -> dict[str, Any]:
        captured_payload.update(payload)
        return {
            "request_id": "req-del-1",
            "output": {
                "voice": "bob_new",
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
        result = await repo.delete_voice_clone(
            provider="dashscope",
            provider_voice_id="bob_new",
        )
    finally:
        monkeypatch.undo()

    assert captured_start["name"] == "voice.clone.delete"
    assert captured_start["model"] == "qwen-voice-enrollment"
    assert observation.ended is True
    assert observation.updates[-1]["usage_details"] == {
        "input": 1,
        "total": 1,
        "unit": "REQUESTS",
    }
    assert captured_payload["model"] == "qwen-voice-enrollment"
    assert captured_payload["input"]["action"] == "delete"
    assert captured_payload["input"]["voice"] == "bob_new"
    assert result == {"provider_request_id": "req-del-1"}


@pytest.mark.asyncio
async def test_list_system_voices_returns_all_voices_and_caches() -> None:
    repo = _build_repo_for_clone()

    result_1 = await repo.list_system_voices(
        provider="dashscope",
        model="qwen3-tts-instruct-flash-realtime",
    )
    result_2 = await repo.list_system_voices(
        provider="dashscope",
        model="qwen3-tts-instruct-flash-realtime",
    )

    assert len(result_1) == 24
    cherry = next(v for v in result_1 if v["provider_voice_id"] == "Cherry")
    assert cherry["display_name"] == "芊悦"
    assert cherry["provider_model"] == "qwen3-tts-instruct-flash-realtime"
    assert result_2 == result_1


@pytest.mark.asyncio
async def test_list_system_voices_raises_for_unsupported_model() -> None:
    repo = _build_repo_for_clone()

    with pytest.raises(APIError) as exc:
        await repo.list_system_voices(
            provider="dashscope",
            model="unknown-tts-model",
        )

    assert exc.value.status_code == 400
    assert exc.value.code == "voice_model_unsupported"


