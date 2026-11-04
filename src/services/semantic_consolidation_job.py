"""Daily semantic consolidation job.

Runs Memory.consolidate(user_id, character_id) for all (user, character) pairs
that have at least one ACTIVE chat.

Important: consolidation must be scoped per (user_id, character_id) to avoid
mixing different users/characters into the same semantic memory set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.memory_system import ConsolidationStats
from src.repositories.chat_repository import ChatRepository
from src.repositories.memory_repository import MemoryRepository

logger = logging.getLogger(__name__)


@dataclass
class SemanticConsolidationStats:
    pairs_total: int = 0
    pairs_succeeded: int = 0
    pairs_failed: int = 0
    memories_processed_total: int = 0
    semantic_created_total: int = 0
    # Phase18: structured per-batch stats (before/after counts, action counts,
    # duration, batch fingerprint). Old totals above stay authoritative for
    # existing consumers; evaluation reads the structured list.
    batch_stats: list[ConsolidationStats] = field(default_factory=list)


class SemanticConsolidationJob:
    """Consolidate semantic memories once per day."""

    # 64-bit advisory lock key. Any stable constant is fine.
    _LOCK_KEY = 202602170300  # "2026-02-17 03:00" as a readable constant

    def __init__(
        self,
        *,
        chat_repo: ChatRepository,
        memory_repo: MemoryRepository,
        per_pair_timeout_s: float = 300.0,
    ) -> None:
        self._chat_repo = chat_repo
        self._memory_repo = memory_repo
        self._per_pair_timeout_s = per_pair_timeout_s

    async def run_once(self) -> SemanticConsolidationStats:
        """Run a single consolidation pass for all ACTIVE chat pairs.

        Uses a Postgres advisory lock so multi-worker deployments do not run
        duplicate jobs.
        """
        stats = SemanticConsolidationStats()
        got_lock = await self._chat_repo.try_advisory_lock(key=self._LOCK_KEY)
        if not got_lock:
            logger.info("Semantic consolidation: skipped (advisory lock held by another worker)")
            return stats

        try:
            pairs = await self._chat_repo.list_active_user_character_pairs()
            stats.pairs_total = len(pairs)

            logger.info("Semantic consolidation: start, active_pairs=%s", stats.pairs_total)

            for user_id, character_id in pairs:
                try:
                    pair_stats = await self._memory_repo.consolidate(
                        user_id=user_id,
                        character_id=character_id,
                        timeout_s=self._per_pair_timeout_s,
                    )
                    stats.pairs_succeeded += 1
                    stats.memories_processed_total += int(pair_stats.memories_processed)
                    stats.semantic_created_total += int(pair_stats.semantic_created)
                    stats.batch_stats.append(pair_stats)
                except Exception as e:
                    stats.pairs_failed += 1
                    logger.warning(
                        "Semantic consolidation: failed for user_id=%s character_id=%s err=%s",
                        user_id,
                        character_id,
                        e,
                    )

            logger.info(
                "Semantic consolidation: done, pairs_total=%s pairs_ok=%s pairs_failed=%s "
                "memories_processed_total=%s semantic_created_total=%s",
                stats.pairs_total,
                stats.pairs_succeeded,
                stats.pairs_failed,
                stats.memories_processed_total,
                stats.semantic_created_total,
            )
            return stats
        finally:
            try:
                await self._chat_repo.advisory_unlock(key=self._LOCK_KEY)
            except Exception:
                logger.exception("Semantic consolidation: failed to release advisory lock cleanly")
