# pyright: reportArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.api.response import APIError
from src.db.models import AuthorType
from src.services import turn_service as turn_service_module
from src.services.turn_service import (
    TurnService,
    _RegenPrep,
)


async def _single_chunk_stream(text: str):
    yield text


async def _empty_tts_stream_from_segments(*, segments_queue, **kwargs):
    while True:
        segment = await segments_queue.get()
        if segment is None:
            break
    if False:
        yield {}


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
async def test_generate_feedback_card_raises_api_error_when_learning_service_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TurnService.__new__(TurnService)
    user_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    workflow_state = {"active": False}

    service._turn_repo = SimpleNamespace(
        get_by_id=AsyncMock(
            return_value=SimpleNamespace(
                id=turn_id,
                chat_id=chat_id,
                author_type=AuthorType.USER.value,
                primary_candidate_id=candidate_id,
            )
        )
    )
    service._chat_repo = SimpleNamespace(
        get_by_id=AsyncMock(
            return_value=SimpleNamespace(
                id=chat_id,
                user_id=user_id,
                active_leaf_turn_id=turn_id,
            )
        )
    )
    service._candidate_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(content="i want go now"))
    )
    service._chat_learning_service = SimpleNamespace(
        generate_feedback_card=AsyncMock(return_value=None),
        resolve_favorite_state=AsyncMock(),
        attach_feedback_card_favorite=AsyncMock(),
    )

    monkeypatch.setattr(
        turn_service_module, "_is_turn_on_active_branch", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        turn_service_module,
        "_load_llm_history_messages_from_leaf",
        AsyncMock(return_value=[{"role": "user", "content": "i want go now"}]),
    )
    monkeypatch.setattr(
        turn_service_module,
        "start_workflow_observation",
        lambda **kwargs: _workflow_observation_stub(workflow_state),
    )
    monkeypatch.setattr(
        turn_service_module,
        "current_trace_context",
        lambda observation=None: {"trace_id": "trace-regen", "parent_span_id": "span-regen"},
    )

    async def _generate_feedback_card(**kwargs):
        assert workflow_state["active"] is True
        return None

    service._chat_learning_service.generate_feedback_card = _generate_feedback_card

    with pytest.raises(APIError) as exc_info:
        await service.generate_feedback_card(
            current_user=SimpleNamespace(id=user_id),
            turn_id=turn_id,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "feedback_card_generation_failed"




@pytest.mark.asyncio
async def test_stream_regen_with_prep_skips_memory_when_memory_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TurnService.__new__(TurnService)
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
    service._candidate_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=None))
    service._turn_repo = SimpleNamespace()
    service._chat_repo = SimpleNamespace()
    service._uow = SimpleNamespace(rollback=AsyncMock())
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
    service._upsert_candidate_extra = AsyncMock()
    service._finalize_regen_success = AsyncMock(return_value=None)

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
    monkeypatch.setattr(turn_service_module, "safe_memory_search", safe_memory_search)
    monkeypatch.setattr(turn_service_module, "spawn_bg_task", spawn_bg_task)
    monkeypatch.setattr(
        turn_service_module,
        "start_workflow_observation",
        lambda **kwargs: _workflow_observation_stub(workflow_state),
    )
    monkeypatch.setattr(
        turn_service_module,
        "current_trace_context",
        lambda observation=None: {"trace_id": "trace-regen", "parent_span_id": "span-regen"},
    )
    monkeypatch.setattr(
        turn_service_module,
        "_load_llm_history_messages_from_leaf",
        AsyncMock(return_value=[]),
    )

    prep = _RegenPrep(
        chat_id=uuid.uuid4(),
        character=SimpleNamespace(
            id=uuid.uuid4(),
            system_prompt="stay in character",
            voice_provider_voice_id="",
            voice_model="",
        ),
        resolved_llm_route=SimpleNamespace(
            provider="openrouter",
            model="test-model",
            model_type_label="test-model",
        ),
        assistant_turn_id=uuid.uuid4(),
        assistant_candidate_id=uuid.uuid4(),
        assistant_candidate_no=1,
        history_leaf_turn_id=None,
        parent_user_candidate_id=uuid.uuid4(),
        user_text="hello there",
    )

    events = [
        event
        async for event in service.stream_regen_with_prep(
            prep=prep,
            current_user=SimpleNamespace(id=uuid.uuid4()),
        )
    ]

    safe_memory_search.assert_not_awaited()
    spawn_bg_task.assert_not_called()
    assert any('"type": "done"' in event for event in events)
    prompt_text = captured_messages["messages"][0]["content"]
    assert "unused-episodic-marker" not in prompt_text
    assert "unused-semantic-marker" not in prompt_text






@pytest.mark.asyncio
async def test_stream_regen_with_prep_triggers_memory_manage_when_memory_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TurnService.__new__(TurnService)
    service._user_settings_repo = SimpleNamespace(
        get_or_create=AsyncMock(
            return_value=SimpleNamespace(
                memory_enabled=True,
                auto_read_aloud_enabled=False,
                mixed_input_auto_translate_enabled=False,
                preferred_expression_bias_enabled=False,
                reply_card_enabled=False,
                display_mode="concise",
            )
        )
    )
    service._subscription_service = SimpleNamespace(has_feature=AsyncMock(return_value=True))
    service._voice_service = None
    service._candidate_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=None))
    service._turn_repo = SimpleNamespace()
    service._chat_repo = SimpleNamespace()
    service._uow = SimpleNamespace(rollback=AsyncMock())
    service._memory_repo = SimpleNamespace(
        allowed_memory_search_timeout_s=1.0,
        use_all_semantic=False,
        manage=AsyncMock(return_value=None),
        judge_and_assign_narrative_group=AsyncMock(return_value=None),
    )
    service._chat_learning_service = SimpleNamespace(
        contains_chinese=Mock(return_value=False),
        load_preferred_expressions=AsyncMock(return_value=[]),
        generate_reply_suggestions=AsyncMock(return_value=[]),
        generate_reply_card=AsyncMock(return_value=None),
    )
    service._upsert_candidate_extra = AsyncMock()
    service._finalize_regen_success = AsyncMock(return_value=None)

    class _CharacterLLMServiceStub:
        def chat_stream_messages(self, *, messages, route, **kwargs):
            return _single_chunk_stream("assistant reply")

    service._character_llm_service = _CharacterLLMServiceStub()

    safe_memory_search = AsyncMock(
        return_value={
            "episodic": [SimpleNamespace(text="unused-episodic-marker")],
            "semantic": [SimpleNamespace(text="unused-semantic-marker")],
        }
    )
    spawned_coroutines: list = []

    def _capture_spawn(coro, *, name, ctx=None):
        spawned_coroutines.append(coro)
        return Mock()

    monkeypatch.setattr(turn_service_module, "safe_memory_search", safe_memory_search)
    monkeypatch.setattr(turn_service_module, "spawn_bg_task", _capture_spawn)
    monkeypatch.setattr(
        turn_service_module,
        "_load_llm_history_messages_from_leaf",
        AsyncMock(return_value=[]),
    )

    prep = _RegenPrep(
        chat_id=uuid.uuid4(),
        character=SimpleNamespace(
            id=uuid.uuid4(),
            system_prompt="stay in character",
            voice_provider_voice_id="",
            voice_model="",
        ),
        resolved_llm_route=SimpleNamespace(
            provider="openrouter",
            model="test-model",
            model_type_label="test-model",
        ),
        assistant_turn_id=uuid.uuid4(),
        assistant_candidate_id=uuid.uuid4(),
        assistant_candidate_no=1,
        history_leaf_turn_id=None,
        parent_user_candidate_id=uuid.uuid4(),
        user_text="hello there",
    )

    _ = [
        event
        async for event in service.stream_regen_with_prep(
            prep=prep,
            current_user=SimpleNamespace(id=uuid.uuid4()),
        )
    ]

    await asyncio.gather(*spawned_coroutines)

    safe_memory_search.assert_awaited_once()
    service._memory_repo.manage.assert_awaited_once()
    service._memory_repo.judge_and_assign_narrative_group.assert_awaited_once()
