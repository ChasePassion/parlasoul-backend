"""Pairwise order-swap normalization and fail-closed denominator rules."""

from __future__ import annotations

from typing import Any

from src.evaluation.constants import ResponseStatus
from src.evaluation.judges.llm_judge import JudgeCallOutcome
from src.evaluation.judges.pairwise import judge_pairwise_with_swap


class _StubJudge:
    """Returns scripted winners keyed by which variant is shown as Response 1."""

    def __init__(self, outcomes: list[JudgeCallOutcome]) -> None:
        self._outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def compare(self, **kwargs: Any) -> JudgeCallOutcome:
        self.calls.append(kwargs)
        return self._outcomes[len(self.calls) - 1]


def _valid(winner_position: str) -> JudgeCallOutcome:
    return JudgeCallOutcome(
        response_status="valid",
        raw_text="{}",
        score=None,
        ooc=None,
        winner_position=winner_position,
        rationale="r",
        error_code=None,
        duration_ms=10,
    )


def _status(status: ResponseStatus) -> JudgeCallOutcome:
    return JudgeCallOutcome(
        response_status=status,
        raw_text=None,
        score=None,
        ooc=None,
        winner_position=None,
        rationale=None,
        error_code="llm_service_error" if status == "judge_error" else "external_timeout",
        duration_ms=10,
    )


async def _run(outcomes: list[JudgeCallOutcome]):
    judge = _StubJudge(outcomes)
    result = await judge_pairwise_with_swap(
        judge,  # type: ignore[arg-type]
        character_snapshot={"name": "Alex"},
        history_turns=[],
        user_message="hi",
        variant_a_content="CONTENT_A",
        variant_b_content="CONTENT_B",
    )
    return judge, result


async def test_consistent_a_win() -> None:
    judge, result = await _run([_valid("response_1"), _valid("response_2")])
    assert result.response_status == "valid"
    assert result.winner == "a"
    assert result.swap_consistent is True
    # second call shows B first
    assert judge.calls[1]["response_1"] == "CONTENT_B"


async def test_consistent_b_win() -> None:
    _, result = await _run([_valid("response_2"), _valid("response_1")])
    assert result.winner == "b"
    assert result.swap_consistent is True


async def test_consistent_tie() -> None:
    _, result = await _run([_valid("tie"), _valid("tie")])
    assert result.winner == "tie"
    assert result.swap_consistent is True


async def test_inconsistent_swap_is_excluded() -> None:
    _, result = await _run([_valid("response_1"), _valid("response_1")])
    assert result.response_status == "judge_inconsistency"
    assert result.winner is None
    assert result.swap_consistent is False


async def test_first_call_error_short_circuits() -> None:
    judge, result = await _run([_status("judge_timeout")])
    assert result.response_status == "judge_timeout"
    assert result.winner is None
    assert result.second is None
    assert len(judge.calls) == 1


async def test_second_call_error_reported() -> None:
    _, result = await _run([_valid("response_1"), _status("judge_parse_failed")])
    assert result.response_status == "judge_parse_failed"
    assert result.winner is None
    assert result.swap_consistent is None
