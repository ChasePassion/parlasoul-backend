"""Shared helpers for replay runners."""

from __future__ import annotations

import hashlib
from typing import Any

from src.evaluation.constants import DIMENSIONS
from src.evaluation.datasets.fingerprints import content_fingerprint
from src.evaluation.datasets.schema import GoldenCase, MemorySnapshot
from src.memory_system import MemoryRecord


def history_messages_from_case(case: GoldenCase) -> list[dict[str, Any]]:
    return [{"role": turn.role, "content": turn.content} for turn in case.history_turns]


def memory_records_from_snapshot(
    snapshot: MemorySnapshot | None,
) -> tuple[dict[str, list[MemoryRecord]], dict[int, str]]:
    """Convert a dataset snapshot into prompt-injectable records.

    Returns ``(memories, id_map)`` where ``id_map`` maps the positional int id
    used at runtime back to the stable dataset ``memory_id``.
    """
    memories: dict[str, list[MemoryRecord]] = {"episodic": [], "semantic": []}
    id_map: dict[int, str] = {}
    if snapshot is None:
        return memories, id_map
    counter = 0
    for bucket in ("episodic", "semantic"):
        items = getattr(snapshot, bucket)
        for item in items:
            counter += 1
            memories[bucket].append(
                MemoryRecord(
                    id=counter,
                    user_id="synthetic",
                    character_id="synthetic",
                    memory_type=bucket,
                    ts=int(item.created_at.timestamp()),
                    chat_id="synthetic",
                    text=item.text,
                )
            )
            id_map[counter] = item.memory_id
    return memories, id_map


def output_ref_for_text(text: str) -> str:
    return content_fingerprint(text)


def rationale_hash(rationale: str | None) -> str | None:
    """Irreversible stand-in for rationale under content_mode=none."""
    if rationale is None:
        return None
    return hashlib.sha256(rationale.encode("utf-8")).hexdigest()


async def judge_all_dimensions(
    judge: Any,
    *,
    case: GoldenCase,
    response: str,
) -> tuple[dict[str, int], str | None, str | None, list[str]]:
    """Score the five dimensions on one response.

    Returns ``(scores, worst_response_status, combined_rationale, failed_dims)``.
    A missing/failed dimension is never filled with a default score.
    """
    scores: dict[str, int] = {}
    failed: list[str] = []
    worst: str | None = None
    rationale_parts: list[str] = []
    common = {
        "character_snapshot": case.character_snapshot,
        "history_turns": history_messages_from_case(case),
        "user_message": case.user_message,
        "response": response,
        "rubric_context": case.expected_behavior,
    }
    for dimension in DIMENSIONS:
        outcome = await judge.score_dimension(dimension=dimension, **common)
        if outcome.response_status == "valid" and outcome.score is not None:
            scores[dimension] = outcome.score
            if outcome.rationale:
                rationale_parts.append(f"[{dimension}] {outcome.rationale}")
        else:
            failed.append(dimension)
            if worst is None:
                worst = outcome.response_status
    combined = "\n".join(rationale_parts) if rationale_parts else None
    return scores, worst, combined, failed
