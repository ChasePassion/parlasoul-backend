# pyright: reportArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.db.models import User
from src.services import chat_service as chat_service_module
from src.services.chat_service import (
    ChatService,
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
            "episodic": [SimpleNamespace(text="unused-episodic-marker")],
            "semantic": [SimpleNamespace(text="unused-semantic-marker")],
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
    prompt_text = captured_messages["messages"][0]["content"]
    assert "unused-episodic-marker" not in prompt_text
    assert "unused-semantic-marker" not in prompt_text




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
