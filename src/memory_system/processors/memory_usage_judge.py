"""Judge which retrieved episodic memories were actually used."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.core.llm_registry import ResolvedLLMRoute

from ..prompts import MEMORY_RELEVANCE_FILTER_PROMPT

if TYPE_CHECKING:
    from src.services.llm_gateway_service import LLMGatewayService

logger = logging.getLogger(__name__)

MEMORY_USAGE_TRACE_VERSION = "1"


@dataclass(frozen=True)
class MemoryUsageTrace:
    """Structured used-memory provenance (phase18 hook).

    ``used_ids`` come from the judge's structured output, never from reply
    text heuristics. ``judge_status='unavailable'`` means the judge did not
    produce a usable answer; consumers must not substitute zeros.
    """

    trace_version: str
    retrieved_ids: tuple[int, ...]
    used_ids: tuple[int, ...]
    judge_status: str  # "ok" | "unavailable"
    prompt_hash: str
    duration_ms: int


def memory_usage_prompt_hash() -> str:
    return hashlib.sha256(MEMORY_RELEVANCE_FILTER_PROMPT.encode("utf-8")).hexdigest()


class MemoryUsageJudge:
    """Judge which episodic memory ids were actually used in a reply."""

    def __init__(self, gateway_service: LLMGatewayService, route: ResolvedLLMRoute) -> None:
        self._gateway_service = gateway_service
        self._route = route

    async def judge_used_memory_ids(
        self,
        episodic_memories: list[dict[str, Any]],
        last_user: str,
        last_assistant: str,
        *,
        user_id: str,
        chat_id: str | None,
        character_id: str,
        trace_sink: Callable[[MemoryUsageTrace], None] | None = None,
    ) -> list[int]:
        started_at = time.monotonic()
        prompt_hash = memory_usage_prompt_hash()

        def _emit_trace(retrieved_ids: list[int], used_ids: list[int], judge_status: str) -> None:
            if trace_sink is None:
                return
            try:
                trace_sink(
                    MemoryUsageTrace(
                        trace_version=MEMORY_USAGE_TRACE_VERSION,
                        retrieved_ids=tuple(retrieved_ids),
                        used_ids=tuple(used_ids),
                        judge_status=judge_status,
                        prompt_hash=prompt_hash,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                    )
                )
            except Exception:  # pragma: no cover - sink must never break production
                logger.warning("Memory usage trace sink failed", exc_info=True)

        if not episodic_memories:
            _emit_trace([], [], "unavailable")
            return []

        items: list[dict[str, Any]] = []
        for mem in episodic_memories:
            if not isinstance(mem, dict):
                continue
            raw_id = mem.get("id")
            raw_text = mem.get("text")
            try:
                mem_id = int(raw_id)
            except Exception:
                continue
            text = str(raw_text or "").strip()
            if not text:
                continue
            items.append({"id": mem_id, "text": text})

        if not items:
            _emit_trace([], [], "unavailable")
            return []

        retrieved_ids = [int(item["id"]) for item in items]

        try:
            response = await self._gateway_service.generate_json(
                route=self._route,
                system_prompt=MEMORY_RELEVANCE_FILTER_PROMPT,
                user_message=json.dumps(
                    {
                        "episodic_memories": [m["text"] for m in items],
                        "last_user": last_user,
                        "last_assistant": last_assistant,
                    },
                    ensure_ascii=False,
                ),
                default={"used_episodic_memories": []},
                operation="memory.group_judge.generate",
                route_role="memory",
                user_id=user_id,
                chat_id=chat_id,
                character_id=character_id,
            )
        except Exception as exc:
            logger.warning("Failed to judge memory usage: %s", exc)
            _emit_trace(retrieved_ids, [], "unavailable")
            return []

        used_texts = response.get("used_episodic_memories", [])
        text_to_ids: dict[str, list[int]] = {}
        for item in items:
            text_to_ids.setdefault(item["text"], []).append(int(item["id"]))

        used_ids: list[int] = []
        seen: set[int] = set()
        if isinstance(used_texts, list):
            for text_value in used_texts:
                text = str(text_value or "").strip()
                if not text:
                    continue
                for mem_id in text_to_ids.get(text, []):
                    if mem_id in seen:
                        continue
                    used_ids.append(mem_id)
                    seen.add(mem_id)
                    break

        logger.info(
            "Memory usage judgment: %s/%s episodic memories were actually used",
            len(used_ids),
            len(items),
        )
        _emit_trace(retrieved_ids, used_ids, "ok")
        return used_ids
