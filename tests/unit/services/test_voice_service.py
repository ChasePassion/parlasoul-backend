from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import APIError
from src.db.models import AuthorType, Candidate, Chat, Turn, User
from src.repositories.candidate_repository import CandidateRepository
from src.repositories.voice_gateway_repository import VoiceGatewayRepository
from src.services.voice_service import VoiceService


def _build_user(user_id: uuid.UUID | None = None) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        email="user@example.com",
        created_at=datetime.now(UTC),
    )


def _build_chat(user_id: uuid.UUID) -> Chat:
    return Chat(
        id=uuid.uuid4(),
        user_id=user_id,
        character_id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_turn(chat_id: uuid.UUID, *, author_type: str) -> Turn:
    return Turn(
        id=uuid.uuid4(),
        chat_id=chat_id,
        turn_no=1,
        author_type=author_type,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_candidate(turn_id: uuid.UUID, content: str = "hello") -> Candidate:
    return Candidate(
        id=uuid.uuid4(),
        turn_id=turn_id,
        candidate_no=1,
        content=content,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


async def _gateway_tts_gen():
    yield "AQI=", "audio/mpeg"
    yield "AwQ=", "audio/mpeg"


async def _gateway_tts_gen_first_chunk_then_error():
    yield "AQI=", "audio/mpeg"
    raise APIError(
        status_code=502,
        code="tts_upstream_error",
        message="upstream error: simulated mid-stream failure",
    )


def _fake_tts_gateway(**methods):
    return SimpleNamespace(**methods)


@pytest.mark.asyncio
async def test_transcribe_audio_delegates_to_gateway() -> None:
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway.transcribe_audio = AsyncMock(return_value=("hello world", "req-1"))
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    service = VoiceService(gateway, candidate_repo)

    result = await service.transcribe_audio(
        audio_bytes=b"abc",
        audio_format="wav",
        sample_rate=16000,
    )

    assert result.text == "hello world"
    assert result.model == "qwen3-asr-flash-realtime"
    gateway.transcribe_audio.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_assistant_candidate_audio_stream_success() -> None:
    user = _build_user()
    chat = _build_chat(user.id)
    turn = _build_turn(chat.id, author_type=AuthorType.CHARACTER.value)
    cand = _build_candidate(turn.id, content="great to see you")

    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway_calls = {"count": 0}

    def _stream_tts_audio_base64(**kwargs):
        gateway_calls["count"] += 1
        return _gateway_tts_gen()

    tts_gateway = _fake_tts_gateway(stream_tts_audio_base64=_stream_tts_audio_base64)

    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    candidate_repo.get_assistant_candidate_with_chat = AsyncMock(return_value=(cand, turn, chat))
    candidate_repo.get_candidate_with_turn_and_chat = AsyncMock()

    service = VoiceService(gateway, candidate_repo, tts_gateway_service=tts_gateway)
    mime, stream = await service.get_assistant_candidate_audio_stream(
        assistant_candidate_id=cand.id,
        current_user=user,
        audio_format="mp3",
    )

    chunks = []
    async for chunk in stream:
        chunks.append(chunk)

    assert mime == "audio/mpeg"
    assert b"".join(chunks) == b"\x01\x02\x03\x04"
    assert gateway_calls["count"] == 1


@pytest.mark.asyncio
async def test_get_assistant_candidate_audio_stream_raises_when_first_chunk_fails() -> None:
    user = _build_user()
    chat = _build_chat(user.id)
    turn = _build_turn(chat.id, author_type=AuthorType.CHARACTER.value)
    cand = _build_candidate(turn.id, content="hello")

    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)

    async def _fail_before_first_chunk():
        raise APIError(
            status_code=502,
            code="tts_upstream_error",
            message="upstream error: simulated init failure",
        )
        yield "AQI=", "audio/mpeg"

    tts_gateway = _fake_tts_gateway(
        stream_tts_audio_base64=lambda **kwargs: _fail_before_first_chunk()
    )

    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    candidate_repo.get_assistant_candidate_with_chat = AsyncMock(return_value=(cand, turn, chat))
    candidate_repo.get_candidate_with_turn_and_chat = AsyncMock()
    service = VoiceService(gateway, candidate_repo, tts_gateway_service=tts_gateway)

    with pytest.raises(APIError) as exc:
        await service.get_assistant_candidate_audio_stream(
            assistant_candidate_id=cand.id,
            current_user=user,
            audio_format="mp3",
        )

    assert exc.value.status_code == 502
    assert exc.value.code == "tts_upstream_error"


@pytest.mark.asyncio
async def test_get_assistant_candidate_audio_stream_swallows_mid_stream_api_error() -> None:
    user = _build_user()
    chat = _build_chat(user.id)
    turn = _build_turn(chat.id, author_type=AuthorType.CHARACTER.value)
    cand = _build_candidate(turn.id, content="hello")

    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    tts_gateway = _fake_tts_gateway(
        stream_tts_audio_base64=lambda **kwargs: _gateway_tts_gen_first_chunk_then_error()
    )

    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    candidate_repo.get_assistant_candidate_with_chat = AsyncMock(return_value=(cand, turn, chat))
    candidate_repo.get_candidate_with_turn_and_chat = AsyncMock()

    service = VoiceService(gateway, candidate_repo, tts_gateway_service=tts_gateway)
    mime, stream = await service.get_assistant_candidate_audio_stream(
        assistant_candidate_id=cand.id,
        current_user=user,
        audio_format="mp3",
    )

    chunks = []
    async for chunk in stream:
        chunks.append(chunk)

    assert mime == "audio/mpeg"
    assert chunks == [b"\x01\x02"]


@pytest.mark.asyncio
async def test_get_assistant_candidate_audio_stream_404() -> None:
    user = _build_user()
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    candidate_repo.get_assistant_candidate_with_chat = AsyncMock(return_value=None)
    candidate_repo.get_candidate_with_turn_and_chat = AsyncMock(return_value=None)
    service = VoiceService(gateway, candidate_repo)

    with pytest.raises(APIError) as exc:
        await service.get_assistant_candidate_audio_stream(
            assistant_candidate_id=uuid.uuid4(),
            current_user=user,
            audio_format="mp3",
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_assistant_candidate_audio_stream_403_when_not_owner() -> None:
    user = _build_user()
    other_user = _build_user()
    chat = _build_chat(other_user.id)
    turn = _build_turn(chat.id, author_type=AuthorType.CHARACTER.value)
    cand = _build_candidate(turn.id)

    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    candidate_repo.get_assistant_candidate_with_chat = AsyncMock(return_value=None)
    candidate_repo.get_candidate_with_turn_and_chat = AsyncMock(return_value=(cand, turn, chat))

    service = VoiceService(gateway, candidate_repo)
    with pytest.raises(APIError) as exc:
        await service.get_assistant_candidate_audio_stream(
            assistant_candidate_id=cand.id,
            current_user=user,
            audio_format="mp3",
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_assistant_candidate_audio_stream_409_when_not_assistant() -> None:
    user = _build_user()
    chat = _build_chat(user.id)
    turn = _build_turn(chat.id, author_type=AuthorType.USER.value)
    cand = _build_candidate(turn.id)

    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    candidate_repo.get_assistant_candidate_with_chat = AsyncMock(return_value=None)
    candidate_repo.get_candidate_with_turn_and_chat = AsyncMock(return_value=(cand, turn, chat))

    service = VoiceService(gateway, candidate_repo)
    with pytest.raises(APIError) as exc:
        await service.get_assistant_candidate_audio_stream(
            assistant_candidate_id=cand.id,
            current_user=user,
            audio_format="mp3",
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_stream_tts_sse_events_emits_done() -> None:
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    captured_kwargs: dict[str, object] = {}

    async def _gen():
        yield "AQI=", "audio/pcm;rate=24000"

    def _stream_tts_audio_base64(**kwargs):
        captured_kwargs.update(kwargs)
        return _gen()

    tts_gateway = _fake_tts_gateway(stream_tts_audio_base64=_stream_tts_audio_base64)
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    service = VoiceService(gateway, candidate_repo, tts_gateway_service=tts_gateway)

    events = []
    async for item in service.stream_tts_sse_events(
        text="hello",
        assistant_candidate_id=str(uuid.uuid4()),
    ):
        events.append(item)

    assert events[0]["type"] == "tts_audio_delta"
    assert events[-1]["type"] == "tts_audio_done"
    assert captured_kwargs["audio_format"] == "pcm"


@pytest.mark.asyncio
async def test_stream_tts_sse_events_from_segments_emits_done() -> None:
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    captured_kwargs: dict[str, object] = {}

    async def _gen():
        yield "AQI=", "audio/pcm;rate=24000"

    def _stream_tts_audio_base64_from_segments(**kwargs):
        captured_kwargs.update(kwargs)
        return _gen()

    tts_gateway = _fake_tts_gateway(
        stream_tts_audio_base64_from_segments=_stream_tts_audio_base64_from_segments
    )
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    service = VoiceService(gateway, candidate_repo, tts_gateway_service=tts_gateway)
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    await queue.put("hello")
    await queue.put(None)

    events = []
    async for item in service.stream_tts_sse_events_from_segments(
        segments_queue=queue,
        assistant_candidate_id=str(uuid.uuid4()),
    ):
        events.append(item)

    assert events[0]["type"] == "tts_audio_delta"
    assert events[0]["seq"] == 1
    assert events[-1]["type"] == "tts_audio_done"
    assert captured_kwargs["audio_format"] == "pcm"
    assert captured_kwargs["segments_queue"] is queue


@pytest.mark.asyncio
async def test_warm_tts_sse_events_from_segments_delegates_to_gateway() -> None:
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    captured_kwargs: dict[str, object] = {}

    async def _warm_tts_audio_base64_from_segments(**kwargs):
        captured_kwargs.update(kwargs)
        return True

    tts_gateway = _fake_tts_gateway(
        warm_tts_audio_base64_from_segments=_warm_tts_audio_base64_from_segments
    )
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    service = VoiceService(gateway, candidate_repo, tts_gateway_service=tts_gateway)

    warmed = await service.warm_tts_sse_events_from_segments(
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model="speech-2.8-turbo",
        voice_source_type="system",
    )

    assert warmed is True
    assert captured_kwargs["audio_format"] == "pcm"
    assert captured_kwargs["provider"] == "minimax"
    assert captured_kwargs["provider_voice_id"] == "Chinese (Mandarin)_Wise_Women"
