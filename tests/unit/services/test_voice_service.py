from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.config import settings
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


def _build_http_gateway() -> VoiceGatewayRepository:
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway.transcribe_audio_http = AsyncMock(return_value=("hello world", "req-1"))
    return gateway




@pytest.mark.asyncio
async def test_transcribe_audio_keeps_websocket_transport_for_opus() -> None:
    gateway = VoiceGatewayRepository.__new__(VoiceGatewayRepository)
    gateway.transcribe_audio = AsyncMock(return_value=("hello world", "req-2"))
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    service = VoiceService(gateway, candidate_repo)

    result = await service.transcribe_audio(
        audio_bytes=b"abc",
        audio_format="opus",
        sample_rate=16000,
    )

    assert result.text == "hello world"
    assert result.model == settings.DASHSCOPE_STT_MODEL
    gateway.transcribe_audio.assert_awaited_once()


def _build_context_repos(
    *,
    user_id: uuid.UUID,
    chat_id: uuid.UUID,
    owner_id: uuid.UUID | None = None,
):
    character_id = uuid.uuid4()
    chat_repo = SimpleNamespace(
        get_by_id=AsyncMock(
            return_value=SimpleNamespace(
                id=chat_id,
                user_id=owner_id if owner_id is not None else user_id,
                character_id=character_id,
                active_leaf_turn_id=None,
                last_turn_id=None,
            )
        )
    )
    character_repo = SimpleNamespace(
        get_by_id=AsyncMock(
            return_value=SimpleNamespace(name="芊悦", description="一位温柔的图书管理员")
        )
    )
    return chat_repo, character_repo




@pytest.mark.asyncio
async def test_stt_context_skips_chat_owned_by_another_user() -> None:
    gateway = _build_http_gateway()
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    user_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    chat_repo, character_repo = _build_context_repos(
        user_id=user_id,
        chat_id=chat_id,
        owner_id=uuid.uuid4(),
    )
    service = VoiceService(gateway, candidate_repo, character_repo, chat_repo)

    await service.transcribe_audio(
        audio_bytes=b"abc",
        audio_format="wav",
        sample_rate=16000,
        chat_id=chat_id,
        current_user_id=user_id,
    )

    assert gateway.transcribe_audio_http.await_args.kwargs["context_messages"] == []






@pytest.mark.asyncio
async def test_stt_context_starts_with_user_and_respects_turn_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import chat_service

    async def _fake_history(turn_repo, candidate_repo, chat_id, leaf_turn_id, limit):
        return [
            {"role": "assistant", "content": "你好，我是芊悦"},
            {"role": "user", "content": "今天天气不错" * 100},
            {"role": "assistant", "content": "是啊" * 100},
        ]

    monkeypatch.setattr(
        chat_service, "_load_llm_history_messages_from_leaf", _fake_history
    )
    gateway = _build_http_gateway()
    candidate_repo = CandidateRepository.__new__(CandidateRepository)
    user_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    chat_repo, character_repo = _build_context_repos(user_id=user_id, chat_id=chat_id)
    service = VoiceService(
        gateway, candidate_repo, character_repo, chat_repo, object()
    )

    await service.transcribe_audio(
        audio_bytes=b"abc",
        audio_format="wav",
        sample_rate=16000,
        chat_id=chat_id,
        current_user_id=user_id,
    )

    context = gateway.transcribe_audio_http.await_args.kwargs["context_messages"]
    assert [item["role"] for item in context] == ["user", "assistant"]
    assert "今天天气不错" in context[0]["text"]
    assert all(len(item["text"]) <= 200 for item in context)
    assert len(context[0]["text"]) + len(context[1]["text"]) <= 400


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


