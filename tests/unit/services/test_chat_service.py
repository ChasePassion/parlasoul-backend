# pyright: reportArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import openai
import pytest

from src.db.models import User
from src.memory_system.exceptions import LLMAuthenticationError
from src.services import chat_service as chat_service_module
from src.services.chat_generation_service import ChatGenerationContext, build_llm_system_prompt
from src.services.chat_service import (
    ChatService,
    _materialize_chat_image_refs,
    _sanitize_candidate_extra,
    _StreamPrep,
)


def _build_user(user_id: uuid.UUID | None = None) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        email="user@example.com",
        created_at=datetime.now(UTC),
    )


async def _single_chunk_stream(text: str):
    yield text


def _spawn_bg_task_stub(coro, *, name, ctx=None):
    coro.close()
    return Mock()


@contextmanager
def _workflow_observation_stub(state: dict[str, bool]):
    state["active"] = True
    try:
        yield Mock()
    finally:
        state["active"] = False


def test_sanitize_candidate_extra_keeps_learning_data_but_strips_sentence_level_ipa() -> None:
    extra = {
        "input_transform": {"applied": True, "transformed_content": "Hi there"},
        "reply_card": {
            "surface": "Hi there",
            "zh": "你好呀",
            "ipa_us": "/haɪ ðer/",
            "key_phrases": [
                {"surface": "hi there", "zh": "你好呀", "ipa_us": "/haɪ ðer/"},
            ],
        },
        "source": "stream_placeholder",
    }

    sanitized = _sanitize_candidate_extra(extra, include_learning_data=True)

    assert sanitized is not None
    assert sanitized is not extra
    assert "input_transform" in sanitized
    assert "reply_card" in sanitized
    assert "reply_card" in sanitized
    assert "ipa_us" not in sanitized["reply_card"]
    assert sanitized["reply_card"]["key_phrases"][0]["ipa_us"] == "/haɪ ðer/"


def test_sanitize_candidate_extra_drops_learning_data_when_disabled() -> None:
    extra = {
        "input_transform": {"applied": True, "transformed_content": "Hi there"},
        "reply_card": {"surface": "Hi there", "zh": "你好呀", "ipa_us": "/haɪ ðer/"},
        "source": "stream_placeholder",
    }

    sanitized = _sanitize_candidate_extra(extra, include_learning_data=False)

    assert sanitized == {"source": "stream_placeholder"}


def test_sanitize_candidate_extra_returns_none_when_only_learning_fields_exist() -> None:
    extra = {
        "input_transform": {"applied": True, "transformed_content": "Hi there"},
        "reply_card": {"surface": "Hi there", "zh": "你好呀", "ipa_us": "/haɪ ðer/"},
    }

    sanitized = _sanitize_candidate_extra(extra, include_learning_data=False)

    assert sanitized is None


def test_build_llm_system_prompt_injects_dialogue_style_without_memory() -> None:
    prompt = build_llm_system_prompt(
        ChatGenerationContext(
            character_system_prompt="Stay in character.",
            dialogue_style_id="spring_breeze",
            history_messages=[],
            user_message="hi",
            memories={"episodic": [], "semantic": []},
            use_all_semantic=False,
            preferred_expressions=[],
            memory_active=False,
        )
    )

    assert "Stay in character." in prompt
    assert "warmer, gentler, and more patient tone" in prompt
    assert "Retrieved memories" not in prompt


@pytest.mark.asyncio
async def test_stream_chat_with_prep_rolls_back_before_finalize_when_unexpected_error() -> None:
    service = ChatService.__new__(ChatService)
    service._user_settings_repo = type("UserSettingsRepoStub", (), {})()
    service._user_settings_repo.get_or_create = AsyncMock(side_effect=RuntimeError("boom"))
    service._voice_service = None
    service._uow = type("UowStub", (), {})()
    service._uow.rollback = AsyncMock()
    service._finalize_stream_error = AsyncMock()

    prep = _StreamPrep(
        chat=object(),
        chat_id=uuid.uuid4(),
        character=object(),
        character_id=uuid.uuid4(),
        resolved_llm_route=object(),
        history_leaf_turn_id=None,
        should_generate_title=False,
        user_turn_id=uuid.uuid4(),
        user_turn_no=1,
        user_candidate_id=uuid.uuid4(),
        assistant_turn_id=uuid.uuid4(),
        assistant_turn_no=2,
        assistant_candidate_id=uuid.uuid4(),
    )
    user = _build_user()

    events: list[str] = []
    async for item in service.stream_chat_with_prep(
        prep=prep,
        current_user=user,
        content="hello",
    ):
        events.append(item)

    service._uow.rollback.assert_awaited_once()
    service._finalize_stream_error.assert_awaited_once()
    assert any('"type": "error"' in event and "chat_stream_failed" in event for event in events)


@pytest.mark.asyncio
async def test_finalize_stream_error_swallows_secondary_db_errors() -> None:
    service = ChatService.__new__(ChatService)
    service._candidate_repo = type("CandidateRepoStub", (), {})()
    service._candidate_repo.get_by_id = AsyncMock(return_value=None)
    service._turn_repo = type("TurnRepoStub", (), {})()
    service._turn_repo.get_by_id = AsyncMock(return_value=None)
    service._chat_repo = type("ChatRepoStub", (), {})()
    service._chat_repo.get_by_id = AsyncMock(return_value=None)
    service._uow = type("UowStub", (), {})()
    service._uow.commit = AsyncMock(side_effect=RuntimeError("tx is broken"))
    service._uow.rollback = AsyncMock()

    chat_id = uuid.uuid4()
    prep = _StreamPrep(
        chat=object(),
        chat_id=chat_id,
        character=object(),
        character_id=uuid.uuid4(),
        resolved_llm_route=object(),
        history_leaf_turn_id=None,
        should_generate_title=False,
        user_turn_id=uuid.uuid4(),
        user_turn_no=1,
        user_candidate_id=uuid.uuid4(),
        assistant_turn_id=uuid.uuid4(),
        assistant_turn_no=2,
        assistant_candidate_id=uuid.uuid4(),
    )

    await service._finalize_stream_error(
        prep=prep,
        code="chat_stream_failed",
        message="chat stream failed",
        partial_content="",
    )

    service._uow.commit.assert_awaited_once()
    service._uow.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_chat_with_prep_skips_memory_when_memory_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService.__new__(ChatService)
    service._user_settings_repo = SimpleNamespace(
        get_or_create=AsyncMock(
            return_value=SimpleNamespace(
                memory_enabled=False,
                auto_read_aloud_enabled=False,
                mixed_input_auto_translate_enabled=False,
                preferred_expression_bias_enabled=False,
                reply_card_enabled=False,
                display_mode="concise",
            )
        )
    )
    service._subscription_service = SimpleNamespace(has_feature=AsyncMock(return_value=False))
    service._voice_service = None
    service._memory_repo = SimpleNamespace(
        allowed_memory_search_timeout_s=1.0,
        use_all_semantic=False,
        manage=AsyncMock(),
        judge_and_assign_narrative_group=AsyncMock(),
    )
    service._chat_learning_service = SimpleNamespace(
        contains_chinese=Mock(return_value=False),
        load_preferred_expressions=AsyncMock(return_value=[]),
        generate_reply_suggestions=AsyncMock(return_value=[]),
        generate_reply_card=AsyncMock(return_value=None),
    )
    service._load_llm_history_messages_from_leaf = AsyncMock(return_value=[])
    service._upsert_candidate_extra = AsyncMock()
    service._finalize_stream_success = AsyncMock(return_value=None)

    captured_messages: dict[str, list[dict[str, str]]] = {}
    workflow_state = {"active": False}

    class _CharacterLLMServiceStub:
        def chat_stream_messages(self, *, messages, route, **kwargs):
            assert workflow_state["active"] is True
            captured_messages["messages"] = messages
            return _single_chunk_stream("assistant reply")

    service._character_llm_service = _CharacterLLMServiceStub()

    safe_memory_search = AsyncMock(
        return_value={
            "episodic": [SimpleNamespace(text="memory")],
            "semantic": [SimpleNamespace(text="fact")],
        }
    )
    spawn_bg_task = Mock(side_effect=_spawn_bg_task_stub)
    monkeypatch.setattr(chat_service_module, "safe_memory_search", safe_memory_search)
    monkeypatch.setattr(chat_service_module, "spawn_bg_task", spawn_bg_task)
    monkeypatch.setattr(
        chat_service_module,
        "start_workflow_observation",
        lambda **kwargs: _workflow_observation_stub(workflow_state),
    )
    monkeypatch.setattr(
        chat_service_module,
        "current_trace_context",
        lambda observation=None: {"trace_id": "trace-1", "parent_span_id": "span-1"},
    )

    chat_id = uuid.uuid4()
    character_id = uuid.uuid4()
    prep = _StreamPrep(
        chat=SimpleNamespace(id=chat_id),
        chat_id=chat_id,
        character=SimpleNamespace(
            id=character_id,
            system_prompt="stay in character",
            voice_provider_voice_id="",
            voice_model="",
        ),
        character_id=character_id,
        resolved_llm_route=SimpleNamespace(
            provider="openrouter",
            model="test-model",
            model_type_label="test-model",
        ),
        history_leaf_turn_id=None,
        should_generate_title=False,
        user_turn_id=uuid.uuid4(),
        user_turn_no=1,
        user_candidate_id=uuid.uuid4(),
        assistant_turn_id=uuid.uuid4(),
        assistant_turn_no=2,
        assistant_candidate_id=uuid.uuid4(),
    )

    events = [
        event
        async for event in service.stream_chat_with_prep(
            prep=prep,
            current_user=_build_user(),
            content="hello there",
        )
    ]

    safe_memory_search.assert_not_awaited()
    spawn_bg_task.assert_not_called()
    assert any('"type": "done"' in event for event in events)
    assert "Retrieved memories" not in captured_messages["messages"][0]["content"]
    assert service._upsert_candidate_extra.await_count == 2
    persisted_payloads = [
        call.kwargs["payload"] for call in service._upsert_candidate_extra.await_args_list[:2]
    ]
    assert persisted_payloads == [
        {"observability_context": {"trace_id": "trace-1", "parent_span_id": "span-1"}},
        {"observability_context": {"trace_id": "trace-1", "parent_span_id": "span-1"}},
    ]
    persisted_commits = [
        call.kwargs.get("commit", False)
        for call in service._upsert_candidate_extra.await_args_list[:2]
    ]
    assert persisted_commits == [False, True]


def test_llm_authentication_error_public_message_includes_provider_specific_hint() -> None:
    response = httpx.Response(
        status_code=401,
        request=httpx.Request(
            "POST",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ),
    )
    original = openai.AuthenticationError(
        "Error code: 401",
        response=response,
        body={
            "error": {
                "type": "authorized_error",
                "message": "login fail: missing token",
            }
        },
    )

    error = LLMAuthenticationError(
        model="qwen3.5-flash",
        provider="dashscope",
        original_error=original,
        detail="login fail: missing token",
    )

    assert (
        error.public_message() == "upstream authentication failed: llm credentials were rejected; "
        "model=qwen3.5-flash; verify llm api key/base url configuration; "
        "detail=login fail: missing token"
    )


@pytest.mark.asyncio
async def test_schedule_chat_tts_warmup_when_auto_read_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService.__new__(ChatService)
    warm_tts = AsyncMock(return_value=True)
    service._voice_service = SimpleNamespace(
        warm_tts_sse_events_from_segments=warm_tts,
    )
    service._user_settings_repo = SimpleNamespace(
        get_by_user_id=AsyncMock(return_value=SimpleNamespace(auto_read_aloud_enabled=True))
    )

    spawn_bg_task = Mock(side_effect=_spawn_bg_task_stub)
    monkeypatch.setattr(chat_service_module, "spawn_bg_task", spawn_bg_task)

    chat_id = uuid.uuid4()
    character_id = uuid.uuid4()
    await service._schedule_chat_tts_warmup(
        current_user=_build_user(),
        chat=SimpleNamespace(id=chat_id),
        character=SimpleNamespace(
            id=character_id,
            voice_provider="minimax",
            voice_provider_voice_id="Chinese (Mandarin)_Wise_Women",
            voice_model="speech-2.8-turbo",
            voice_source_type="system",
        ),
    )

    warm_tts.assert_called_once_with(
        provider="minimax",
        provider_voice_id="Chinese (Mandarin)_Wise_Women",
        provider_model="speech-2.8-turbo",
        voice_source_type="system",
    )
    spawn_bg_task.assert_called_once()


def test_materialize_chat_image_refs_backfills_legacy_url_payloads() -> None:
    refs = _materialize_chat_image_refs(
        [
            {
                "original": "https://cdn.example.com/images/avatars/chat/user-1/image-1/original",
                "display": "/media/images/avatars/chat/user-1/image-1/display.avif",
            }
        ]
    )

    assert refs is not None
    assert refs[0].image_key == "images/avatars/chat/user-1/image-1"
    assert refs[0].display == "/media/images/avatars/chat/user-1/image-1/display.avif"


@pytest.mark.asyncio
async def test_stream_chat_with_prep_uses_vision_route_for_image_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ChatService.__new__(ChatService)
    service._user_settings_repo = SimpleNamespace(
        get_or_create=AsyncMock(
            return_value=SimpleNamespace(
                memory_enabled=False,
                auto_read_aloud_enabled=False,
                mixed_input_auto_translate_enabled=False,
                preferred_expression_bias_enabled=False,
                reply_card_enabled=False,
                display_mode="concise",
            )
        )
    )
    service._subscription_service = SimpleNamespace(has_feature=AsyncMock(return_value=False))
    service._voice_service = None
    service._memory_repo = SimpleNamespace(
        allowed_memory_search_timeout_s=1.0,
        use_all_semantic=False,
        manage=AsyncMock(),
        judge_and_assign_narrative_group=AsyncMock(),
    )
    service._chat_learning_service = SimpleNamespace(
        contains_chinese=Mock(return_value=False),
        load_preferred_expressions=AsyncMock(return_value=[]),
        generate_reply_suggestions=AsyncMock(return_value=[]),
        generate_reply_card=AsyncMock(return_value=None),
    )
    service._load_llm_history_messages_from_leaf = AsyncMock(
        return_value=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "/media/images/avatars/chat/user-1/image-1/original"}},
                    {"type": "text", "text": "看看这张图"},
                ],
            }
        ]
    )
    service._upsert_candidate_extra = AsyncMock()
    service._finalize_stream_success = AsyncMock(return_value=None)

    text_route = SimpleNamespace(
        provider="deepseek",
        model="deepseek-v4-flash",
        model_type_label="deepseek:chat:deepseek-v4-flash",
    )
    vision_route = SimpleNamespace(
        provider="dashscope",
        model="qwen-vl-plus",
        model_type_label="dashscope:vision:qwen-vl-plus",
    )
    captured: dict[str, object] = {}
    workflow_state = {"active": False}

    class _CharacterLLMServiceStub:
        def resolve_vision_route(self):
            return vision_route

        def chat_stream_messages(self, *, messages, route, **kwargs):
            assert workflow_state["active"] is True
            captured["messages"] = messages
            captured["route"] = route
            return _single_chunk_stream("assistant reply")

    service._character_llm_service = _CharacterLLMServiceStub()

    monkeypatch.setattr(chat_service_module, "spawn_bg_task", Mock(side_effect=_spawn_bg_task_stub))
    monkeypatch.setattr(
        chat_service_module,
        "start_workflow_observation",
        lambda **kwargs: _workflow_observation_stub(workflow_state),
    )
    monkeypatch.setattr(
        chat_service_module,
        "current_trace_context",
        lambda observation=None: {"trace_id": "trace-1", "parent_span_id": "span-1"},
    )

    chat_id = uuid.uuid4()
    character_id = uuid.uuid4()
    prep = _StreamPrep(
        chat=SimpleNamespace(id=chat_id),
        chat_id=chat_id,
        character=SimpleNamespace(
            id=character_id,
            system_prompt="stay in character",
            voice_provider_voice_id="",
            voice_model="",
        ),
        character_id=character_id,
        resolved_llm_route=text_route,
        history_leaf_turn_id=None,
        should_generate_title=False,
        user_turn_id=uuid.uuid4(),
        user_turn_no=1,
        user_candidate_id=uuid.uuid4(),
        assistant_turn_id=uuid.uuid4(),
        assistant_turn_no=2,
        assistant_candidate_id=uuid.uuid4(),
    )

    events = [
        event
        async for event in service.stream_chat_with_prep(
            prep=prep,
            current_user=_build_user(),
            content="继续说",
        )
    ]

    assert any('"type": "done"' in event for event in events)
    assert captured["route"] is vision_route
