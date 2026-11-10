"""Runner + pipeline contract tests with stub judge/generation/writer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.evaluation.judges.llm_judge import JudgeCallOutcome
from src.evaluation.metrics.report import ReportWriteError, ReportWriter

RUBRIC = "parlasoul-roleplay-5d-ooc-v1"


def _outcome_stub(
    *,
    status: str = "valid",
    score: int | None = 4,
    ooc: bool | None = None,
    winner: str | None = None,
    supported_claims: tuple[str, ...] = (),
    abstained: bool | None = None,
    rationale: str | None = "observed evidence",
    error_code: str | None = None,
) -> JudgeCallOutcome:
    return JudgeCallOutcome(
        response_status=status,  # type: ignore[arg-type]
        raw_text=None,
        score=score,
        ooc=ooc,
        winner_position=winner,
        rationale=rationale,
        error_code=error_code,
        duration_ms=5,
        supported_claims=supported_claims,
        abstained=abstained,
    )


class StubJudge:
    provider = "stubjudge"
    model = "stub-judge-model"
    rubric_version = RUBRIC

    def __init__(
        self,
        *,
        dimension_status: str = "valid",
        ooc_status: str = "valid",
        ooc_label: bool = False,
        compare_sequence: list[str] | None = None,
        supported_claims: tuple[str, ...] = (),
        abstained: bool = True,
    ) -> None:
        self.dimension_status = dimension_status
        self.ooc_status = ooc_status
        self.ooc_label = ooc_label
        self.compare_sequence = list(compare_sequence or [])
        self.supported_claims = supported_claims
        self.abstained = abstained

    def prompt_hash_for(self, kind: str) -> str:
        return "c" * 64

    async def score_dimension(self, **_: Any) -> JudgeCallOutcome:
        return _outcome_stub(
            status=self.dimension_status,
            score=4 if self.dimension_status == "valid" else None,
        )

    async def classify_ooc(self, **_: Any) -> JudgeCallOutcome:
        return _outcome_stub(status=self.ooc_status, ooc=self.ooc_label, score=None)

    async def compare(self, **_: Any) -> JudgeCallOutcome:
        winner = self.compare_sequence.pop(0) if self.compare_sequence else "tie"
        if winner == "__parse_failed__":
            return _outcome_stub(status="judge_parse_failed", winner=None, score=None)
        return _outcome_stub(status="valid", winner=winner, score=None)

    async def judge_claims(self, **_: Any) -> JudgeCallOutcome:
        return _outcome_stub(status="valid", score=None, supported_claims=self.supported_claims)

    async def judge_abstention(self, **_: Any) -> JudgeCallOutcome:
        return _outcome_stub(status="valid", score=None, abstained=self.abstained)




# -- ChatReplayRunner -------------------------------------------------------






# -- PairwiseReplayRunner ---------------------------------------------------




# -- MemoryReplayRunner -----------------------------------------------------




# -- Candidate / Realtime runners --------------------------------------------




# -- Pipeline ----------------------------------------------------------------




# -- Report -------------------------------------------------------------------




async def test_report_writer_fails_closed_on_raw_text(tmp_path: Path) -> None:
    from src.evaluation.runners.base import CaseOutcome

    outcome = CaseOutcome(
        case_id="raw-1",
        runner="chat_replay",
        status="completed",
        response_status="valid",
        input_ref="case:raw-1",
        output_ref="case:raw-1",
        rule_metrics={"leak": "This is a long raw sentence with spaces."},
    )
    writer = ReportWriter(out_dir=tmp_path, content_mode="none")
    with pytest.raises(ReportWriteError, match="sanitizer"):
        writer.write_run_report(
            run_id="run-2", run_metadata={}, outcomes=[outcome], summary={}
        )
