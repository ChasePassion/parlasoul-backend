from __future__ import annotations

import base64
import json
import wave
from io import BytesIO
from typing import Any

import httpx
import pytest

from src.core.exceptions import APIError
from src.repositories import voice_gateway_repository as voice_gateway_module
from src.repositories.voice_gateway_repository import VoiceGatewayRepository


def _build_repo() -> VoiceGatewayRepository:
    repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    repo._api_key = "sk-test"
    repo._billing_region = "cn"
    repo._stt_http_model = "qwen-audio-3.1-asr-flash"
    repo._stt_http_api_url = "https://example.com/asr"
    repo._stt_http_timeout_seconds = 5.0
    repo._stt_language_hints = ["zh", "en"]
    return repo


def _success_payload() -> dict[str, Any]:
    return {
        "output": {
            "text": "你好，芊悦",
            "sentence": {"text": "你好，芊悦"},
        },
        "usage": {
            "duration": 4,
            "input_tokens": 12,
            "output_tokens": 5,
            "total_tokens": 17,
        },
        "request_id": "req-http-1",
    }


def _install_http_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int = 200,
    payload: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _Response:
        def __init__(self) -> None:
            self.status_code = status_code
            self.text = json.dumps(payload or {}, ensure_ascii=False)

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            calls.append({"timeout": timeout})

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> _Response:
            calls.append({"url": url, "json": json, "headers": headers})
            if error is not None:
                raise error
            return _Response()

    monkeypatch.setattr(voice_gateway_module.httpx, "AsyncClient", _Client)
    return calls


def _capture_observation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {"start": {}, "updates": []}

    def _start(**kwargs: Any) -> object:
        captured["start"] = kwargs
        return object()

    def _update(observation: Any, **kwargs: Any) -> None:
        captured["updates"].append(kwargs)

    monkeypatch.setattr(voice_gateway_module, "_start_voice_model_observation", _start)
    monkeypatch.setattr(voice_gateway_module, "update_observation_safe", _update)
    return captured


@pytest.mark.asyncio
async def test_http_stt_builds_expected_request_and_extracts_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_http_client(monkeypatch, payload=_success_payload())
    repo = _build_repo()

    text, request_id = await repo.transcribe_audio_http(
        audio_bytes=b"RIFF-fake-wav",
        audio_format="wav",
        sample_rate=16000,
        context_messages=[
            {"role": "user", "text": "角色名：芊悦"},
            {"role": "assistant", "text": "你好，我是芊悦"},
        ],
    )

    assert text == "你好，芊悦"
    assert request_id == "req-http-1"

    call = calls[-1]
    assert call["url"] == "https://example.com/asr"
    assert call["headers"]["X-DashScope-SSE"] == "disable"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["headers"]["Content-Type"] == "application/json"

    body = call["json"]
    assert body["model"] == "qwen-audio-3.1-asr-flash"
    assert body["parameters"] == {
        "format": "wav",
        "sample_rate": "16000",
        "language_hints": ["zh", "en"],
    }

    messages = body["input"]["messages"]
    assert len(messages) == 3
    assert messages[0]["content"][0]["type"] == "input_text"
    assert messages[1]["content"][0]["type"] == "text"
    assert messages[2]["role"] == "user"
    audio_content = messages[2]["content"][0]
    assert audio_content["type"] == "input_audio"
    data_uri = audio_content["input_audio"]["data"]
    assert data_uri.startswith("data:audio/wav;base64,")
    assert base64.b64decode(data_uri.split(",", 1)[1]) == b"RIFF-fake-wav"


@pytest.mark.asyncio
async def test_http_stt_records_token_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_client(monkeypatch, payload=_success_payload())
    captured = _capture_observation(monkeypatch)
    repo = _build_repo()

    await repo.transcribe_audio_http(
        audio_bytes=b"RIFF-fake-wav",
        audio_format="wav",
        sample_rate=16000,
    )

    assert captured["start"]["model"] == "qwen-audio-3.1-asr-flash"
    assert captured["start"]["metadata"]["billing_unit"] == "TOKENS"
    assert captured["start"]["metadata"]["source_unit"] == "TOKENS"
    assert captured["start"]["metadata"]["transport"] == "http"
    update = captured["updates"][-1]
    assert update["usage_details"] == {"input": 12, "output": 5, "total": 17}
    assert update["metadata"]["audio_duration_seconds"] == 4
    assert update["output"] == {"text_chars": 5, "request_id": "req-http-1"}
    assert "你好" not in json.dumps(captured, ensure_ascii=False)


@pytest.mark.asyncio
async def test_http_stt_wraps_pcm_into_wav(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_http_client(monkeypatch, payload=_success_payload())
    repo = _build_repo()
    pcm = b"\x01\x02" * 160

    await repo.transcribe_audio_http(
        audio_bytes=pcm,
        audio_format="pcm",
        sample_rate=16000,
    )

    data_uri = calls[-1]["json"]["input"]["messages"][-1]["content"][0][
        "input_audio"
    ]["data"]
    with wave.open(BytesIO(base64.b64decode(data_uri.split(",", 1)[1])), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.readframes(wav.getnframes()) == pcm


@pytest.mark.asyncio
async def test_http_stt_context_text_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_http_client(monkeypatch, payload=_success_payload())
    repo = _build_repo()

    await repo.transcribe_audio_http(
        audio_bytes=b"RIFF-fake-wav",
        audio_format="wav",
        sample_rate=16000,
        context_messages=[{"role": "user", "text": "专名" * 500}],
    )

    text = calls[-1]["json"]["input"]["messages"][0]["content"][0]["text"]
    assert len(text) == 400


@pytest.mark.asyncio
async def test_http_stt_empty_text_raises_no_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http_client(
        monkeypatch,
        payload={"output": {"text": ""}, "usage": {"total_tokens": 0}, "request_id": "r"},
    )
    repo = _build_repo()

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio_http(
            audio_bytes=b"RIFF-fake-wav",
            audio_format="wav",
            sample_rate=16000,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "stt_no_speech_detected"


@pytest.mark.asyncio
async def test_http_stt_unreadable_response_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http_client(monkeypatch, payload={})
    repo = _build_repo()

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio_http(
            audio_bytes=b"RIFF-fake-wav",
            audio_format="wav",
            sample_rate=16000,
        )

    assert exc.value.status_code == 502
    assert exc.value.code == "stt_upstream_error"


@pytest.mark.asyncio
async def test_http_stt_non_200_maps_to_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http_client(monkeypatch, status_code=429, payload={"error": "rate limited"})
    repo = _build_repo()

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio_http(
            audio_bytes=b"RIFF-fake-wav",
            audio_format="wav",
            sample_rate=16000,
        )

    assert exc.value.status_code == 502
    assert exc.value.code == "stt_upstream_error"
    assert "rate limited" not in exc.value.message


@pytest.mark.asyncio
async def test_http_stt_timeout_maps_to_external_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http_client(
        monkeypatch,
        error=httpx.TimeoutException("timed out"),
    )
    repo = _build_repo()

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio_http(
            audio_bytes=b"RIFF-fake-wav",
            audio_format="wav",
            sample_rate=16000,
        )

    assert exc.value.status_code == 504
    assert exc.value.code == "external_timeout"


@pytest.mark.asyncio
async def test_http_stt_transport_error_maps_to_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http_client(monkeypatch, error=httpx.ConnectError("connection failed"))
    repo = _build_repo()

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio_http(
            audio_bytes=b"RIFF-fake-wav",
            audio_format="wav",
            sample_rate=16000,
        )

    assert exc.value.status_code == 502
    assert exc.value.code == "stt_upstream_error"


@pytest.mark.asyncio
async def test_http_stt_unsupported_format_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_http_client(monkeypatch, payload=_success_payload())
    repo = _build_repo()

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio_http(
            audio_bytes=b"opus-bytes",
            audio_format="opus",
            sample_rate=16000,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "validation_failed"
    assert len(calls) == 0


@pytest.mark.asyncio
async def test_http_stt_empty_audio_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_client(monkeypatch, payload=_success_payload())
    repo = _build_repo()

    with pytest.raises(APIError) as exc:
        await repo.transcribe_audio_http(
            audio_bytes=b"",
            audio_format="wav",
            sample_rate=16000,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "stt_audio_empty"


@pytest.mark.asyncio
async def test_http_stt_uses_settings_defaults_when_built_with_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_http_client(monkeypatch, payload=_success_payload())
    repo = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    repo._api_key = "sk-test"
    repo._billing_region = "cn"

    await repo.transcribe_audio_http(
        audio_bytes=b"RIFF-fake-wav",
        audio_format="wav",
        sample_rate=16000,
    )

    body = calls[-1]["json"]
    assert body["model"] == "qwen-audio-3.1-asr-flash"
    assert body["parameters"]["language_hints"] == ["zh", "en"]
