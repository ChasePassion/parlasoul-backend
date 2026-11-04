from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.services.proactive_character_selector as proactive_character_selector_module
from src.services.proactive_character_selector import ProactiveCharacterSelector


@pytest.mark.asyncio
async def test_selector_returns_none_when_proactive_disabled() -> None:
    user_id = uuid.uuid4()
    selector = ProactiveCharacterSelector(
        turn_repo=SimpleNamespace(),
        user_settings_repo=SimpleNamespace(
            get_by_user_id=AsyncMock(return_value=SimpleNamespace(proactive_enabled=False))
        ),
        proactive_pref_repo=SimpleNamespace(),
        memory_repo=SimpleNamespace(),
    )

    result = await selector.select(user_id)

    assert result.selected_character_id is None
    assert result.candidates_evaluated == 0
    assert result.weights == {}


@pytest.mark.asyncio
async def test_selector_uses_activity_and_cooldown_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    first_character_id = uuid.uuid4()
    second_character_id = uuid.uuid4()
    now = datetime.now(UTC)

    async def count_memory(*, user_id: str, character_id: str) -> int:
        return 5

    async def count_turns(*, user_id: uuid.UUID, character_id: uuid.UUID, since: datetime, cap: int) -> int:
        return 10 if character_id == first_character_id else 2

    async def last_proactive(*, user_id: uuid.UUID, character_id: uuid.UUID) -> datetime | None:
        if character_id == first_character_id:
            return now - timedelta(days=1)
        return now - timedelta(days=5)

    monkeypatch.setattr(proactive_character_selector_module.random, "uniform", lambda _a, _b: 0.0)

    selector = ProactiveCharacterSelector(
        turn_repo=SimpleNamespace(
            count_user_turns_for_character=AsyncMock(side_effect=count_turns),
            get_last_proactive_outreach_at=AsyncMock(side_effect=last_proactive),
        ),
        user_settings_repo=SimpleNamespace(
            get_by_user_id=AsyncMock(return_value=SimpleNamespace(proactive_enabled=True))
        ),
        proactive_pref_repo=SimpleNamespace(
            list_enabled_character_ids_for_interacted_user=AsyncMock(
                return_value=[first_character_id, second_character_id]
            )
        ),
        memory_repo=SimpleNamespace(
            count_by_user_character=AsyncMock(side_effect=count_memory)
        ),
    )

    result = await selector.select(user_id)

    assert result.selected_character_id == first_character_id
    assert result.candidates_evaluated == 2
    assert result.weights[str(first_character_id)] == 22.0
    assert result.weights[str(second_character_id)] == 18.0
