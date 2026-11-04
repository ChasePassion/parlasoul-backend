"""Scheduler and worker loops for proactive message dispatches."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.core.config import settings
from src.core.dependencies import get_memory_system
from src.core.uow import UnitOfWork
from src.db.session import AsyncSessionLocal
from src.repositories.candidate_repository import CandidateRepository
from src.repositories.character_repository import CharacterRepository
from src.repositories.chat_repository import ChatRepository
from src.repositories.memory_repository import MemoryRepository
from src.repositories.proactive_character_preference_repository import (
    ProactiveCharacterPreferenceRepository,
)
from src.repositories.proactive_dispatch_repository import ProactiveDispatchRepository
from src.repositories.turn_repository import TurnRepository
from src.repositories.user_repository import UserRepository
from src.repositories.user_settings_repository import UserSettingsRepository
from src.services.character_llm_service import CharacterLLMService
from src.services.chat_service import ChatService
from src.services.proactive_character_selector import ProactiveCharacterSelector
from src.services.proactive_message_job import ProactiveMessageJob

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_slot_hours() -> list[int]:
    hours: list[int] = []
    for chunk in settings.PROACTIVE_JOB_SLOT_HOURS.split(","):
        normalized = chunk.strip()
        if not normalized:
            continue
        hour = int(normalized)
        if 0 <= hour <= 23 and hour not in hours:
            hours.append(hour)
    return hours


def _due_slots_for_user(*, now_utc: datetime, timezone_name: str) -> list[tuple[datetime, str]]:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo(settings.USER_SETTINGS_TIMEZONE_DEFAULT)
        timezone_name = settings.USER_SETTINGS_TIMEZONE_DEFAULT

    local_now = now_utc.astimezone(tz)
    scan_window = timedelta(seconds=settings.PROACTIVE_JOB_SCAN_INTERVAL_SECONDS)
    due_slots: list[tuple[datetime, str]] = []
    for hour in _parse_slot_hours():
        slot_local = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if timedelta(0) <= local_now - slot_local < scan_window:
            due_slots.append((slot_local.astimezone(UTC), f"{hour:02d}:00"))
    return due_slots


def _build_proactive_job(db_session) -> ProactiveMessageJob:
    uow = UnitOfWork(db_session)
    chat_repo = ChatRepository(db_session)
    turn_repo = TurnRepository(db_session)
    candidate_repo = CandidateRepository(db_session)
    character_repo = CharacterRepository(db_session)
    memory_repo = MemoryRepository(memory_factory=get_memory_system)
    user_settings_repo = UserSettingsRepository(db_session)
    proactive_pref_repo = ProactiveCharacterPreferenceRepository(db_session)
    character_llm_service = CharacterLLMService()
    chat_service = ChatService(
        uow=uow,
        chat_repo=chat_repo,
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=character_repo,
        memory_repo=memory_repo,
        user_settings_repo=user_settings_repo,
        chat_learning_service=SimpleNamespace(),
        chat_title_service=SimpleNamespace(),
        growth_service=None,
        voice_service=None,
        character_llm_service=character_llm_service,
        subscription_service=SimpleNamespace(),
    )
    selector = ProactiveCharacterSelector(
        turn_repo=turn_repo,
        user_settings_repo=user_settings_repo,
        proactive_pref_repo=proactive_pref_repo,
        memory_repo=memory_repo,
    )
    return ProactiveMessageJob(
        uow=uow,
        dispatch_repo=ProactiveDispatchRepository(db_session),
        user_repo=UserRepository(db_session),
        user_settings_repo=user_settings_repo,
        character_repo=character_repo,
        turn_repo=turn_repo,
        memory_repo=memory_repo,
        selector=selector,
        chat_service=chat_service,
        character_llm_service=character_llm_service,
    )


async def run_proactive_scheduler_once() -> int:
    now = _now_utc()
    inserted = 0
    async with AsyncSessionLocal() as db:
        db.info["current_user_id"] = "system"
        user_settings_repo = UserSettingsRepository(db)
        dispatch_repo = ProactiveDispatchRepository(db)
        users = await user_settings_repo.list_proactive_enabled_users()
        for user_id, timezone_name in users:
            for slot_at, slot_label in _due_slots_for_user(now_utc=now, timezone_name=timezone_name):
                created = await dispatch_repo.enqueue_unique(
                    user_id=user_id,
                    slot_at=slot_at,
                    slot_label=slot_label,
                    timezone=timezone_name,
                    payload_json={"scheduler_enqueued_at": now.isoformat()},
                )
                if created:
                    inserted += 1
        await db.commit()
    logger.info(
        "Proactive scheduler tick completed.",
        extra={"event": "proactive.scheduler.tick"},
    )
    return inserted


async def process_one_proactive_dispatch() -> bool:
    now = _now_utc()
    async with AsyncSessionLocal() as db:
        db.info["current_user_id"] = "system"
        dispatch_repo = ProactiveDispatchRepository(db)
        dispatch = await dispatch_repo.claim_due(
            now=now,
            lease_seconds=settings.PROACTIVE_JOB_DISPATCH_LEASE_SECONDS,
        )
        if dispatch is None:
            await db.rollback()
            return False
        await db.commit()

        job = _build_proactive_job(db)
        await job.process_claimed_dispatch(dispatch)
        await db.commit()
        return True


async def proactive_scheduler_loop() -> None:
    while True:
        try:
            await run_proactive_scheduler_once()
        except Exception:
            logger.exception("Proactive scheduler loop failed")
        await asyncio.sleep(settings.PROACTIVE_JOB_SCAN_INTERVAL_SECONDS)


async def proactive_worker_loop(*, worker_name: str) -> None:
    while True:
        try:
            processed = await process_one_proactive_dispatch()
            if not processed:
                await asyncio.sleep(settings.PROACTIVE_JOB_IDLE_SLEEP_SECONDS)
        except Exception:
            logger.exception("Proactive worker loop failed. worker_name=%s", worker_name)
            await asyncio.sleep(settings.PROACTIVE_JOB_IDLE_SLEEP_SECONDS)
