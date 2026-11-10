from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db.models import Character, User
from src.services.growth_service import GrowthService


def _build_user() -> User:
    return User(id=uuid.uuid4(), email="user@example.com")


def _build_character(character_id: uuid.UUID, *, name: str = "Luna") -> Character:
    return Character(
        id=character_id,
        name=name,
        description="desc",
        system_prompt="prompt",
        greeting_message="Hello there",
        voice_provider="dashscope",
        voice_model="qwen3-tts-instruct-flash-realtime",
        voice_provider_voice_id="Cherry",
        voice_source_type="system",
        visibility="PUBLIC",
        interaction_count=0,
    )


def _build_trigger(*, payload: dict, trigger_kind: str, chat_id, character_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        payload=payload,
        trigger_kind=trigger_kind,
        triggered_at=datetime(2026, 4, 5, 8, 0, 0),
        chat_id=chat_id,
        character_id=character_id,
    )


async def _create_share_trigger(*, user_id, chat_id, character_id, trigger_kind, trigger_key, payload):
    return _build_trigger(
        payload=payload,
        trigger_kind=trigger_kind,
        chat_id=chat_id,
        character_id=character_id,
    )


async def _create_share_trigger_if_absent(**kwargs):
    return await _create_share_trigger(**kwargs), True


async def _none_trigger_by_key(trigger_key: str):
    return None


def _build_service(repo, character_repo) -> GrowthService:
    uow = SimpleNamespace(transactional=None)
    return GrowthService(
        uow=uow,
        growth_repo=repo,
        chat_repo=SimpleNamespace(),
        turn_repo=SimpleNamespace(),
        candidate_repo=SimpleNamespace(),
        character_repo=character_repo,
    )


@pytest.mark.asyncio
async def test_record_greeting_inserted_updates_totals_but_not_signin() -> None:
    user_id = uuid.uuid4()
    character_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    daily_stats = SimpleNamespace(total_message_count=0, total_word_count=0, user_signin_message_count=0)
    character_daily_stats = SimpleNamespace(total_message_count=0, total_word_count=0, total_exchange_count=0, last_chat_at=None)
    character_stats = SimpleNamespace(total_message_count=0, total_word_count=0, total_exchange_count=0, chatted_days_count=0, last_chat_at=None)
    repo = SimpleNamespace(
        get_or_create_daily_stats=AsyncMock(return_value=daily_stats),
        get_or_create_character_daily_stats=AsyncMock(return_value=character_daily_stats),
        get_or_create_character_stats=AsyncMock(return_value=character_stats),
        get_share_trigger_by_key=AsyncMock(side_effect=_none_trigger_by_key),
        create_share_trigger=AsyncMock(side_effect=_create_share_trigger),
    )
    character_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=_build_character(character_id)))
    service = _build_service(repo, character_repo)

    result = await service.record_greeting_inserted(
        user_id=user_id,
        character_id=character_id,
        chat_id=chat_id,
        greeting_text="Hello there",
    )

    assert daily_stats.total_message_count == 1
    assert daily_stats.total_word_count == 2
    assert daily_stats.user_signin_message_count == 0
    assert character_daily_stats.total_exchange_count == 0
    assert character_stats.total_exchange_count == 0
    assert result == []


@pytest.mark.asyncio
async def test_record_canonical_chat_completed_updates_signin_and_milestones() -> None:
    user_id = uuid.uuid4()
    character_id = uuid.uuid4()
    chat_id = uuid.uuid4()
    user_stats = SimpleNamespace(
        current_natural_streak=2,
        longest_natural_streak=2,
        makeup_card_balance=0,
        last_natural_signin_date=date(2026, 4, 4),
        last_rewarded_natural_streak=0,
    )
    daily_stats = SimpleNamespace(
        user_signin_message_count=14,
        total_message_count=28,
        total_word_count=100,
        is_natural_signed=False,
        natural_signed_at=None,
        is_makeup_signed=False,
        makeup_signed_at=None,
    )
    character_daily_stats = SimpleNamespace(
        total_message_count=98,
        total_word_count=500,
        total_exchange_count=0,
        last_chat_at=None,
    )
    character_stats = SimpleNamespace(
        total_message_count=98,
        total_word_count=500,
        total_exchange_count=49,
        chatted_days_count=5,
        last_chat_at=None,
    )
    repo = SimpleNamespace(
        get_or_create_user_stats=AsyncMock(return_value=user_stats),
        get_or_create_daily_stats=AsyncMock(return_value=daily_stats),
        get_or_create_character_daily_stats=AsyncMock(return_value=character_daily_stats),
        get_or_create_character_stats=AsyncMock(return_value=character_stats),
        get_share_trigger_by_key=AsyncMock(side_effect=_none_trigger_by_key),
        create_share_trigger_if_absent=AsyncMock(side_effect=_create_share_trigger_if_absent),
    )
    character_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=_build_character(character_id)))
    service = _build_service(repo, character_repo)

    result = await service.record_canonical_chat_completed(
        user_id=user_id,
        character_id=character_id,
        chat_id=chat_id,
        user_message="Hello there",
        assistant_message="Nice to see you",
        occurred_at=datetime(2026, 4, 5, 9, 0, 0),
    )

    assert daily_stats.user_signin_message_count == 15
    assert daily_stats.is_natural_signed is True
    assert user_stats.current_natural_streak == 3
    assert user_stats.longest_natural_streak == 3
    assert user_stats.makeup_card_balance == 1
    assert user_stats.last_rewarded_natural_streak == 3
    assert character_stats.total_message_count == 100
    assert character_stats.total_exchange_count == 50
    assert character_stats.chatted_days_count == 6
    assert len(result.share_cards) == 2


@pytest.mark.asyncio
async def test_apply_makeup_recalculates_streak_bridging_gap() -> None:
    target_date = date(2026, 5, 7)

    user_stats = SimpleNamespace(
        current_natural_streak=1,
        longest_natural_streak=3,
        makeup_card_balance=2,
        last_natural_signin_date=date(2026, 5, 8),
        last_rewarded_natural_streak=0,
    )
    daily_stats = SimpleNamespace(is_natural_signed=False, is_makeup_signed=False)

    def _list_range(_user_id, *, start_date, end_date, **kwargs):
        rows = []
        current = start_date
        while current <= end_date:
            rows.append(
                SimpleNamespace(
                    stat_date=current,
                    is_natural_signed=current
                    in (date(2026, 5, 5), date(2026, 5, 6), date(2026, 5, 8)),
                    is_makeup_signed=current == date(2026, 5, 7),
                )
            )
            current += timedelta(days=1)
        return rows

    repo = SimpleNamespace(
        get_or_create_user_stats=AsyncMock(return_value=user_stats),
        get_or_create_daily_stats=AsyncMock(return_value=daily_stats),
        list_daily_stats_range=AsyncMock(side_effect=_list_range),
        get_daily_stats=AsyncMock(return_value=SimpleNamespace(
            stat_date=target_date,
            is_natural_signed=False,
            is_makeup_signed=True,
            makeup_signed_at=datetime(2026, 5, 10, 9, 0, 0),
        )),
    )

    class _FakeTx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            pass

    uow = SimpleNamespace(transactional=lambda: _FakeTx())
    service = GrowthService(
        uow=uow,
        growth_repo=repo,
        chat_repo=SimpleNamespace(),
        turn_repo=SimpleNamespace(),
        candidate_repo=SimpleNamespace(),
        character_repo=SimpleNamespace(),
    )

    result = await service.apply_makeup(
        current_user=_build_user(),
        target_date=target_date,
    )

    assert user_stats.makeup_card_balance == 1
    assert user_stats.current_natural_streak == 4
    assert user_stats.longest_natural_streak == 4
    assert user_stats.last_natural_signin_date == date(2026, 5, 8)
    assert result.current_natural_streak == 4


@pytest.mark.asyncio
async def test_apply_makeup_does_not_regress_streak_when_makeup_is_isolated() -> None:
    target_date = date(2026, 5, 9)

    user_stats = SimpleNamespace(
        current_natural_streak=5,
        longest_natural_streak=5,
        makeup_card_balance=2,
        last_natural_signin_date=date(2026, 5, 5),
        last_rewarded_natural_streak=3,
    )
    daily_stats = SimpleNamespace(is_natural_signed=False, is_makeup_signed=False)

    def _list_range(_user_id, *, start_date, end_date, **kwargs):
        rows = []
        current = start_date
        while current <= end_date:
            rows.append(
                SimpleNamespace(
                    stat_date=current,
                    is_natural_signed=current
                    in (date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 4), date(2026, 5, 5)),
                    is_makeup_signed=current == date(2026, 5, 9),
                )
            )
            current += timedelta(days=1)
        return rows

    repo = SimpleNamespace(
        get_or_create_user_stats=AsyncMock(return_value=user_stats),
        get_or_create_daily_stats=AsyncMock(return_value=daily_stats),
        list_daily_stats_range=AsyncMock(side_effect=_list_range),
        get_daily_stats=AsyncMock(return_value=SimpleNamespace(
            stat_date=target_date,
            is_natural_signed=False,
            is_makeup_signed=True,
        )),
    )

    class _FakeTx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            pass

    uow = SimpleNamespace(transactional=lambda: _FakeTx())
    service = GrowthService(
        uow=uow,
        growth_repo=repo,
        chat_repo=SimpleNamespace(),
        turn_repo=SimpleNamespace(),
        candidate_repo=SimpleNamespace(),
        character_repo=SimpleNamespace(),
    )

    await service.apply_makeup(
        current_user=_build_user(),
        target_date=target_date,
    )

    assert user_stats.current_natural_streak == 1
    assert user_stats.longest_natural_streak == 5
    assert user_stats.last_natural_signin_date == date(2026, 5, 9)


