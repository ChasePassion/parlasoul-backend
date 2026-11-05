from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db.models import Character, CharacterStatusType, ProactiveMessageDispatch, User, VisibilityType
from src.services.proactive_character_selector import SelectionResult
from src.services.proactive_message_job import ProactiveMessageJob


def _user() -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid.uuid4(),
        email="user@example.com",
        username="tester",
        created_at=now,
        last_login_at=now,
    )


def _character(character_id: uuid.UUID) -> Character:
    return Character(
        id=character_id,
        name="Luna",
        description="desc",
        system_prompt="prompt",
        greeting_message="hello",
        avatar_image_key=None,
        voice_provider="minimax",
        voice_model="speech-2.8-turbo",
        voice_provider_voice_id="voice-1",
        voice_source_type="system",
        status=CharacterStatusType.ACTIVE.value,
        visibility=VisibilityType.PUBLIC.value,
        creator_id=uuid.uuid4(),
        identifier=None,
    )


def _dispatch(user_id: uuid.UUID) -> ProactiveMessageDispatch:
    now = datetime.now(UTC)
    return ProactiveMessageDispatch(
        id=uuid.uuid4(),
        user_id=user_id,
        slot_at=now,
        slot_label="manual",
        timezone="UTC",
        status="processing",
        attempt_count=1,
        next_attempt_at=now,
        payload_json={},
    )


@pytest.mark.asyncio
async def test_proactive_message_job_marks_dispatch_skipped_when_disabled() -> None:
    user = _user()
    dispatch = _dispatch(user.id)
    dispatch_repo = SimpleNamespace(
        mark_skipped=AsyncMock(),
        mark_succeeded=AsyncMock(),
        schedule_retry=AsyncMock(),
        mark_failed=AsyncMock(),
    )
    job = ProactiveMessageJob(
        uow=SimpleNamespace(),
        dispatch_repo=dispatch_repo,
        user_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=user)),
        user_settings_repo=SimpleNamespace(
            get_or_create=AsyncMock(return_value=SimpleNamespace(proactive_enabled=False))
        ),
        character_repo=SimpleNamespace(),
        turn_repo=SimpleNamespace(get_last_activity_at=AsyncMock(return_value=None)),
        memory_repo=SimpleNamespace(),
        selector=SimpleNamespace(),
        chat_service=SimpleNamespace(),
        character_llm_service=SimpleNamespace(),
    )

    await job.process_claimed_dispatch(dispatch)

    dispatch_repo.mark_skipped.assert_awaited_once()
    assert dispatch_repo.mark_skipped.await_args.kwargs["error_code"] == "resource_conflict"
    dispatch_repo.mark_succeeded.assert_not_awaited()


@pytest.mark.asyncio
async def test_proactive_message_job_success_path_marks_dispatch_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    character_id = uuid.uuid4()
    character = _character(character_id)
    dispatch = _dispatch(user.id)
    dispatch_repo = SimpleNamespace(
        mark_skipped=AsyncMock(),
        mark_succeeded=AsyncMock(),
        schedule_retry=AsyncMock(),
        mark_failed=AsyncMock(),
    )

    chat = SimpleNamespace(id=uuid.uuid4())
    turn = SimpleNamespace(id=uuid.uuid4())
    candidate = SimpleNamespace(id=uuid.uuid4())
    memory_repo = SimpleNamespace(
        list_for_proactive_prompt=AsyncMock(
            return_value={
                "semantic": [
                    SimpleNamespace(text="memory one"),
                    SimpleNamespace(text="memory two"),
                    SimpleNamespace(text="memory three"),
                ],
                "episodic": [],
            }
        ),
        record_proactive_outreach=AsyncMock(),
    )
    chat_service = SimpleNamespace(
        create_proactive_chat=AsyncMock(return_value=(chat, turn, candidate))
    )
    character_llm_service = SimpleNamespace(
        resolve_runtime_route=AsyncMock(return_value=SimpleNamespace()),
        generate_text=AsyncMock(return_value="hello from proactive job"),
    )

    spawned_tasks: list[asyncio.Task[None]] = []

    def _schedule(coro, **_kwargs):
        task = asyncio.create_task(coro)
        spawned_tasks.append(task)
        return task

    monkeypatch.setattr("src.services.proactive_message_job.spawn_bg_task", _schedule)

    job = ProactiveMessageJob(
        uow=SimpleNamespace(),
        dispatch_repo=dispatch_repo,
        user_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=user)),
        user_settings_repo=SimpleNamespace(
            get_or_create=AsyncMock(return_value=SimpleNamespace(proactive_enabled=True))
        ),
        character_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=character)),
        turn_repo=SimpleNamespace(
            get_last_activity_at=AsyncMock(return_value=datetime.now(UTC) - timedelta(days=2)),
            get_last_interaction_at=AsyncMock(return_value=datetime.now(UTC) - timedelta(days=2)),
        ),
        memory_repo=memory_repo,
        selector=SimpleNamespace(
            select=AsyncMock(
                return_value=SelectionResult(
                    selected_character_id=character_id,
                    candidates_evaluated=1,
                    weights={str(character_id): 1.0},
                )
            )
        ),
        chat_service=chat_service,
        character_llm_service=character_llm_service,
    )

    await job.process_claimed_dispatch(dispatch)
    await asyncio.gather(*spawned_tasks)

    chat_service.create_proactive_chat.assert_awaited_once()
    create_kwargs = chat_service.create_proactive_chat.await_args.kwargs
    assert create_kwargs["content"] == "hello from proactive job"
    assert create_kwargs["character"] is character

    memory_repo.record_proactive_outreach.assert_awaited_once_with(
        proactive_message="hello from proactive job",
        user_id=str(user.id),
        character_id=str(character.id),
        chat_id=str(chat.id),
    )

    dispatch_repo.mark_succeeded.assert_awaited_once()
    payload = dispatch_repo.mark_succeeded.await_args.kwargs["payload_json"]
    assert payload["chat_id"] == str(chat.id)
    assert payload["turn_id"] == str(turn.id)
    assert payload["candidate_id"] == str(candidate.id)
    dispatch_repo.mark_failed.assert_not_awaited()
    dispatch_repo.schedule_retry.assert_not_awaited()
