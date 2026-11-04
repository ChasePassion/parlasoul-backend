"""Scheduler for daily semantic consolidation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.core.config import settings
from src.core.dependencies import get_memory_system
from src.db.session import AsyncSessionLocal
from src.repositories.chat_repository import ChatRepository
from src.repositories.memory_repository import MemoryRepository
from src.services.semantic_consolidation_job import SemanticConsolidationJob

logger = logging.getLogger(__name__)


def seconds_until_daily_run(*, tz_name: str, hour: int, minute: int) -> float:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return max(0.0, (run_at - now).total_seconds())


async def semantic_consolidation_scheduler_loop() -> None:
    while True:
        delay = seconds_until_daily_run(
            tz_name=settings.SEMANTIC_JOB_TIMEZONE,
            hour=settings.SEMANTIC_JOB_HOUR,
            minute=settings.SEMANTIC_JOB_MINUTE,
        )
        logger.info(
            "Semantic scheduler: next run in %.0fs (%s %02d:%02d)",
            delay,
            settings.SEMANTIC_JOB_TIMEZONE,
            settings.SEMANTIC_JOB_HOUR,
            settings.SEMANTIC_JOB_MINUTE,
        )
        await asyncio.sleep(delay)
        try:
            async with AsyncSessionLocal() as db:
                db.info["current_user_id"] = "system"
                job = SemanticConsolidationJob(
                    chat_repo=ChatRepository(db),
                    memory_repo=MemoryRepository(get_memory_system()),
                    per_pair_timeout_s=settings.SEMANTIC_JOB_PAIR_TIMEOUT_SECONDS,
                )
                await job.run_once()
        except Exception:
            logger.exception("Semantic consolidation job failed")
