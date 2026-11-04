"""Weighted selector for proactive outreach characters."""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.core.config import settings
from src.repositories.memory_repository import MemoryRepository
from src.repositories.proactive_character_preference_repository import (
    ProactiveCharacterPreferenceRepository,
)
from src.repositories.turn_repository import TurnRepository
from src.repositories.user_settings_repository import UserSettingsRepository

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SelectionResult:
    selected_character_id: uuid.UUID | None
    candidates_evaluated: int
    weights: dict[str, float]


@dataclass(frozen=True)
class _Candidate:
    character_id: uuid.UUID
    weight: float


class ProactiveCharacterSelector:
    def __init__(
        self,
        *,
        turn_repo: TurnRepository,
        user_settings_repo: UserSettingsRepository,
        proactive_pref_repo: ProactiveCharacterPreferenceRepository,
        memory_repo: MemoryRepository,
    ) -> None:
        self._turn_repo = turn_repo
        self._user_settings_repo = user_settings_repo
        self._proactive_pref_repo = proactive_pref_repo
        self._memory_repo = memory_repo

    async def select(self, user_id: uuid.UUID) -> SelectionResult:
        user_settings = await self._user_settings_repo.get_by_user_id(user_id)
        if user_settings is None or not user_settings.proactive_enabled:
            return SelectionResult(None, 0, {})

        enabled_character_ids = (
            await self._proactive_pref_repo.list_enabled_character_ids_for_interacted_user(user_id)
        )
        if not enabled_character_ids:
            return SelectionResult(None, 0, {})

        now = _now_utc()
        since = now - timedelta(days=settings.PROACTIVE_JOB_ACTIVITY_WINDOW_DAYS)
        candidates: list[_Candidate] = []
        weights: dict[str, float] = {}

        for character_id in enabled_character_ids:
            memory_count = await self._memory_repo.count_by_user_character(
                user_id=str(user_id),
                character_id=str(character_id),
            )
            if memory_count < settings.PROACTIVE_JOB_MIN_MEMORY_COUNT:
                continue

            activity = await self._turn_repo.count_user_turns_for_character(
                user_id=user_id,
                character_id=character_id,
                since=since,
                cap=settings.PROACTIVE_JOB_ACTIVITY_CAP,
            )
            last_proactive_at = await self._turn_repo.get_last_proactive_outreach_at(
                user_id=user_id,
                character_id=character_id,
            )
            cooldown_days = settings.PROACTIVE_JOB_COOLDOWN_CAP_DAYS
            if last_proactive_at is not None:
                cooldown_days = min(
                    max(0, (now - last_proactive_at).days),
                    settings.PROACTIVE_JOB_COOLDOWN_CAP_DAYS,
                )

            weight = float((activity + 1) * (cooldown_days + 1))
            candidates.append(_Candidate(character_id=character_id, weight=weight))
            weights[str(character_id)] = weight

        if not candidates:
            return SelectionResult(None, len(enabled_character_ids), weights)

        selected = self._weighted_random_choice(candidates)
        logger.info(
            "Selected proactive character.",
            extra={
                "event": "proactive.selector.selected",
                "user_id": str(user_id),
                "character_id": str(selected.character_id),
            },
        )
        return SelectionResult(
            selected_character_id=selected.character_id,
            candidates_evaluated=len(candidates),
            weights=weights,
        )

    @staticmethod
    def _weighted_random_choice(candidates: list[_Candidate]) -> _Candidate:
        total = sum(candidate.weight for candidate in candidates)
        threshold = random.uniform(0.0, total)
        cumulative = 0.0
        for candidate in candidates:
            cumulative += candidate.weight
            if threshold <= cumulative:
                return candidate
        return candidates[-1]
