"""Blind pairwise judging with mandatory order swap.

Normalization contract (phase18 §3.2):
- two calls under the same locked route/rubric, display order swapped;
- display position is mapped back to canonical ``a|b|tie``;
- only when BOTH calls are strictly valid and agree does the case enter the
  valid denominator; disagreement records ``judge_inconsistency`` and is
  excluded — never silently dropped, never down-weighted without calibration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.evaluation.constants import ResponseStatus
from src.evaluation.judges.llm_judge import JudgeCallOutcome, LLMJudge

CanonicalWinner = Literal["a", "b", "tie"]


@dataclass(frozen=True)
class PairwiseOutcome:
    response_status: ResponseStatus
    winner: CanonicalWinner | None
    swap_consistent: bool | None
    first: JudgeCallOutcome
    second: JudgeCallOutcome | None

    @property
    def error_code(self) -> str | None:
        return self.first.error_code if self.second is None else (
            self.first.error_code or self.second.error_code
        )


def _map_position_to_canonical(
    winner_position: str | None, *, first_is_a: bool
) -> CanonicalWinner | None:
    if winner_position == "tie":
        return "tie"
    if winner_position == "response_1":
        return "a" if first_is_a else "b"
    if winner_position == "response_2":
        return "b" if first_is_a else "a"
    return None


async def judge_pairwise_with_swap(
    judge: LLMJudge,
    *,
    character_snapshot: dict[str, Any],
    history_turns: list[dict[str, Any]],
    user_message: str,
    variant_a_content: str,
    variant_b_content: str,
) -> PairwiseOutcome:
    common = {
        "character_snapshot": character_snapshot,
        "history_turns": history_turns,
        "user_message": user_message,
    }

    first = await judge.compare(
        response_1=variant_a_content, response_2=variant_b_content, **common
    )
    if first.response_status != "valid":
        return PairwiseOutcome(
            response_status=first.response_status,
            winner=None,
            swap_consistent=None,
            first=first,
            second=None,
        )

    second = await judge.compare(
        response_1=variant_b_content, response_2=variant_a_content, **common
    )
    if second.response_status != "valid":
        return PairwiseOutcome(
            response_status=second.response_status,
            winner=None,
            swap_consistent=None,
            first=first,
            second=second,
        )

    winner_first = _map_position_to_canonical(first.winner_position, first_is_a=True)
    winner_second = _map_position_to_canonical(second.winner_position, first_is_a=False)
    if winner_first is None or winner_second is None:
        # Defensive: valid status implies a mapped winner; treat as parse-level failure.
        return PairwiseOutcome(
            response_status="judge_parse_failed",
            winner=None,
            swap_consistent=None,
            first=first,
            second=second,
        )

    if winner_first != winner_second:
        return PairwiseOutcome(
            response_status="judge_inconsistency",
            winner=None,
            swap_consistent=False,
            first=first,
            second=second,
        )

    return PairwiseOutcome(
        response_status="valid",
        winner=winner_first,
        swap_consistent=True,
        first=first,
        second=second,
    )
