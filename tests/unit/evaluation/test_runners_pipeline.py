"""Runner + pipeline contract tests with stub judge/generation/writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from src.db.models import EvalResult, EvalRun
from src.evaluation.datasets.schema import GoldenCase, PairwiseCase
from src.evaluation.judges.llm_judge import JudgeCallOutcome
from src.evaluation.metrics.aggregate import build_case_run_summary
from src.evaluation.metrics.report import ReportWriteError, ReportWriter
from src.evaluation.pipeline import EvalPipeline, finalize_status, outcome_to_eval_result
from src.evaluation.runners.base import CaseExecutionContext
from src.evaluation.runners.candidate_runner import CandidateReplayRunner
from src.evaluation.runners.chat_runner import ChatReplayRunner
from src.evaluation.runners.memory_runner import MemoryReplayRunner
from src.evaluation.runners.pairwise_runner import PairwiseReplayRunner
from src.evaluation.runners.realtime_functional_runner import RealtimeFunctionalRunner
from src.services.chat_generation_service import ChatGenerationResult
from tests.unit.evaluation.fixture_factory import (
    make_memory_case,
    make_pairwise_case,
    make_roleplay_case,
)

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


class StubGeneration:
    def __init__(self, *, text: str = "A calm, in-character reply.", mode: str = "ok") -> None:
        self.text = text
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    async def generate_once(self, context, **kwargs: Any) -> ChatGenerationResult:
        self.calls.append(kwargs)
        if self.mode == "timeout":
            # Emulates the seam's asyncio.wait_for boundary raising TimeoutError.
            raise TimeoutError()
        if self.mode == "error":
            raise RuntimeError("upstream exploded")
        return ChatGenerationResult(
            text=self.text,
            provider="stubgen",
            model="stub-gen-model",
            model_type_label="stubgen:stub-gen-model",
        )


class StubWriter:
    def __init__(self) -> None:
        self.results: list[EvalResult] = []
        self.cancelled: list[str] = []

    async def upsert_result(self, result: EvalResult) -> None:
        self.results.append(result)

    async def mark_cases_cancelled(self, *, case_ids: list[str], **_: Any) -> int:
        self.cancelled.extend(case_ids)
        return len(case_ids)


def _context(content_mode: str = "none", judge=None, generation=None) -> CaseExecutionContext:
    return CaseExecutionContext(
        run_id=uuid4(),
        content_mode=content_mode,  # type: ignore[arg-type]
        judge=judge,
        generation_service=generation,
        generation_route=object(),
        generation_timeout_s=0.05,
    )


def _run_row(content_mode: str = "none") -> EvalRun:
    return EvalRun(
        id=uuid4(),
        name="stub run",
        dataset_id="parlasoul_gold",
        dataset_version="v1.0.0",
        dataset_fingerprint="a" * 64,
        runner_version="parlasoul-eval-runner-0.1.0",
        run_kind="case",
        judge_route="locked",
        effective_judge_provider="stubjudge",
        effective_judge_model="stub-judge-model",
        rubric_version=RUBRIC,
        prompt_template_version="parlasoul-judge-prompt-v1",
        prompt_hash="b" * 64,
        content_mode=content_mode,
        status="running",
    )


# -- ChatReplayRunner -------------------------------------------------------


async def test_chat_replay_happy_path_none_mode_persists_no_text() -> None:
    case = GoldenCase.model_validate(make_roleplay_case("rp-1"))
    judge = StubJudge()
    generation = StubGeneration()
    runner = ChatReplayRunner()

    outcome = await runner.run_case(case, _context(judge=judge, generation=generation))

    assert outcome.status == "completed"
    assert outcome.response_status == "valid"
    assert set(outcome.dimension_scores) == {
        "persona_fidelity",
        "multi_turn_consistency",
        "coherence_progression",
        "empathy_appropriateness",
        "engagement_diversity",
    }
    assert outcome.rule_metrics["ooc"]["predicted"] is False
    assert outcome.rule_metrics["repetition_3gram"]["status"] == "ok"
    assert outcome.generation_provider == "stubgen"
    # generation used the single-attempt seam; capture defaults to on so the
    # Judge prompt/reply stay inspectable in observability
    call = generation.calls[0]
    assert call["retry_policy"] == "none"
    assert call["capture_input"] is True
    assert call["capture_output"] is True

    result = outcome_to_eval_result(outcome, run=_run_row(), content_mode="none")
    assert result.input_text is None
    assert result.actual_output_text is None
    assert result.judge_rationale is not None and len(result.judge_rationale) == 64


async def test_chat_replay_capture_flags_pass_through_from_context() -> None:
    case = GoldenCase.model_validate(make_roleplay_case("rp-cap"))
    generation = StubGeneration()
    context = _context(judge=StubJudge(), generation=generation)
    context = CaseExecutionContext(
        run_id=context.run_id,
        content_mode="none",
        judge=context.judge,
        generation_service=generation,
        generation_route=object(),
        generation_timeout_s=0.05,
        capture_input=False,
        capture_output=False,
    )
    outcome = await ChatReplayRunner().run_case(case, context)
    assert outcome.status == "completed"
    call = generation.calls[0]
    assert call["capture_input"] is False
    assert call["capture_output"] is False


async def test_chat_replay_redacted_mode_keeps_text() -> None:
    case = GoldenCase.model_validate(make_roleplay_case("rp-2"))
    outcome = await ChatReplayRunner().run_case(
        case, _context(content_mode="redacted", judge=StubJudge(), generation=StubGeneration())
    )
    result = outcome_to_eval_result(outcome, run=_run_row("redacted"), content_mode="redacted")
    assert result.input_text == case.user_message
    assert result.actual_output_text == "A calm, in-character reply."


async def test_chat_replay_generation_timeout_is_runner_error() -> None:
    case = GoldenCase.model_validate(make_roleplay_case("rp-3"))
    outcome = await ChatReplayRunner().run_case(
        case, _context(judge=StubJudge(), generation=StubGeneration(mode="timeout"))
    )
    assert outcome.status == "failed"
    assert outcome.response_status == "runner_error"
    assert outcome.error_code == "external_timeout"


async def test_chat_replay_generation_error_is_llm_service_error() -> None:
    case = GoldenCase.model_validate(make_roleplay_case("rp-4"))
    outcome = await ChatReplayRunner().run_case(
        case, _context(judge=StubJudge(), generation=StubGeneration(mode="error"))
    )
    assert outcome.status == "failed"
    assert outcome.error_code == "llm_service_error"
    assert "RuntimeError" not in (outcome.actual_output_text or "")


async def test_chat_replay_judge_parse_failure_marks_case_failed() -> None:
    case = GoldenCase.model_validate(make_roleplay_case("rp-5"))
    judge = StubJudge(dimension_status="judge_parse_failed")
    outcome = await ChatReplayRunner().run_case(
        case, _context(judge=judge, generation=StubGeneration())
    )
    assert outcome.status == "failed"
    assert outcome.response_status == "judge_parse_failed"
    assert outcome.dimension_scores == {}


# -- PairwiseReplayRunner ---------------------------------------------------


def _pairwise_case() -> PairwiseCase:
    base = make_roleplay_case("rp-base")
    return PairwiseCase.model_validate(make_pairwise_case(base, case_id="pair-1"))


async def test_pairwise_consistent_winner_maps_to_a() -> None:
    judge = StubJudge(compare_sequence=["response_1", "response_2"])
    runner = PairwiseReplayRunner(base_cases={"rp-base": GoldenCase.model_validate(
        make_roleplay_case("rp-base")
    )})
    outcome = await runner.run_case(_pairwise_case(), _context(judge=judge))
    assert outcome.response_status == "valid"
    assert outcome.pairwise_winner == "a"
    assert outcome.swap_consistent is True
    assert outcome.variant_a_generation_model == "fixture-model-a"


async def test_pairwise_inconsistency_is_excluded() -> None:
    judge = StubJudge(compare_sequence=["response_1", "response_1"])
    runner = PairwiseReplayRunner(base_cases={"rp-base": GoldenCase.model_validate(
        make_roleplay_case("rp-base")
    )})
    outcome = await runner.run_case(_pairwise_case(), _context(judge=judge))
    assert outcome.response_status == "judge_inconsistency"
    assert outcome.pairwise_winner is None
    assert outcome.swap_consistent is False
    assert outcome.status == "failed"


async def test_pairwise_missing_base_fails_closed() -> None:
    judge = StubJudge()
    runner = PairwiseReplayRunner(base_cases={})
    outcome = await runner.run_case(_pairwise_case(), _context(judge=judge))
    assert outcome.response_status == "runner_error"
    assert outcome.error_code == "validation_failed"


# -- MemoryReplayRunner -----------------------------------------------------


async def test_memory_faithfulness_records_supported_required_rate() -> None:
    case = GoldenCase.model_validate(
        make_memory_case(
            "mem-faith",
            task_type="memory_faithfulness",
            expected_claims=[
                {"claim_id": "claim-1", "text": "works nights", "supporting_memory_ids": ["m-fixture-001"], "is_required": True},
                {"claim_id": "claim-2", "text": "likes tea", "supporting_memory_ids": ["m-fixture-001"], "is_required": False},
            ],
        )
    )
    judge = StubJudge(supported_claims=("claim-1",))
    runner = MemoryReplayRunner(gateway_service=None, judge_route=None)
    outcome = await runner.run_case(case, _context(judge=judge, generation=StubGeneration()))

    faithfulness = outcome.rule_metrics["faithfulness"]
    assert faithfulness["response_status"] == "valid"
    assert faithfulness["supported_required_count"] == 1
    assert faithfulness["required_count"] == 1
    assert faithfulness["rate"] == 1.0
    assert outcome.rule_metrics["memory_usage"]["judge_status"] == "unavailable"


async def test_memory_abstention_records_prediction_and_gold() -> None:
    case = GoldenCase.model_validate(
        make_memory_case("mem-abs", task_type="memory_abstention", gold_memory_ids=[])
        | {"should_abstain": True}
    )
    judge = StubJudge(abstained=True)
    runner = MemoryReplayRunner()
    outcome = await runner.run_case(case, _context(judge=judge, generation=StubGeneration()))
    abstention = outcome.rule_metrics["abstention"]
    assert abstention["predicted"] is True
    assert abstention["gold"] is True
    assert abstention["correct"] is True


async def test_memory_recall_case_reports_hook_required() -> None:
    case = GoldenCase.model_validate(make_memory_case("mem-recall"))
    runner = MemoryReplayRunner()
    outcome = await runner.run_case(case, _context(judge=StubJudge(), generation=StubGeneration()))
    assert outcome.rule_metrics["retrieval_dependent"] == {"status": "hook_required"}
    assert outcome.status == "completed"


# -- Candidate / Realtime runners --------------------------------------------


async def test_candidate_replay_without_source_candidate_is_skipped() -> None:
    # No source_candidate_id -> skipped without touching the database
    # (missing-candidate DB paths are covered by the integration suite).
    case = GoldenCase.model_validate(make_roleplay_case("cand-1"))
    outcome = await CandidateReplayRunner().run_case(case, _context())
    assert outcome.status == "skipped"
    assert outcome.response_status == "skipped"
    assert outcome.error_code == "validation_failed"
    assert "no_source_candidate" in outcome.error_detail_safe


async def test_realtime_scenario_passes_and_records_telemetry() -> None:
    from src.evaluation.datasets.schema import RealtimeScenario

    scenario = RealtimeScenario.model_validate(
        {
            "scenario_id": "rt-1",
            "network_profile": "wifi_normal",
            "actions": [
                {"type": "connect"},
                {"type": "send_user_audio_fixture", "at_ms": 0, "fixture_key": "fx"},
                {"type": "disconnect"},
            ],
            "assertions": [
                {"event": "assistant_audio_started", "within_ms": 1800, "anchor": "send_user_audio_fixture"},
                {"event": "session_ended", "eventually": True},
            ],
            "metrics": ["ttfa_ms"],
        }
    )
    outcome = await RealtimeFunctionalRunner().run_case(scenario, _context())
    assert outcome.status == "completed"
    assert outcome.rule_metrics["scenario_passed"] is True
    assert outcome.rule_metrics["metrics"]["ttfa_ms"] > 0


async def test_realtime_scenario_failed_assertion_recorded() -> None:
    from src.evaluation.datasets.schema import RealtimeScenario

    scenario = RealtimeScenario.model_validate(
        {
            "scenario_id": "rt-2",
            "network_profile": "wifi_normal",
            "actions": [{"type": "connect"}, {"type": "send_user_audio_fixture", "at_ms": 0}],
            "assertions": [
                {"event": "assistant_audio_started", "within_ms": 10, "anchor": "send_user_audio_fixture"},
            ],
            "metrics": [],
        }
    )
    outcome = await RealtimeFunctionalRunner().run_case(scenario, _context())
    assert outcome.status == "completed"
    assert outcome.rule_metrics["scenario_passed"] is False
    assert outcome.rule_metrics["assertions_failed"] == 1


# -- Pipeline ----------------------------------------------------------------


async def test_pipeline_persists_outcomes_and_marks_cancelled() -> None:
    writer = StubWriter()
    pipeline = EvalPipeline(writer=writer, concurrency=2, content_mode="none")
    pipeline.bind(judge=StubJudge(), generation_service=StubGeneration(), generation_route=object())
    run = _run_row()

    cases = [GoldenCase.model_validate(make_roleplay_case(f"p-{i}")) for i in range(3)]
    outcomes = await pipeline.run_cases(run=run, runner=ChatReplayRunner(), cases=cases)

    assert len(outcomes) == 3
    assert len(writer.results) == 3
    assert all(result.run_id == run.id for result in writer.results)
    assert finalize_status(run_kind="case", outcomes=outcomes) == "completed"


async def test_pipeline_cancel_marks_queued_cases_cancelled() -> None:
    writer = StubWriter()
    pipeline = EvalPipeline(writer=writer, concurrency=1, content_mode="none")
    pipeline.bind(judge=StubJudge(), generation_service=StubGeneration(), generation_route=object())
    pipeline.request_cancel()

    cases = [GoldenCase.model_validate(make_roleplay_case(f"c-{i}")) for i in range(2)]
    outcomes = await pipeline.run_cases(run=_run_row(), runner=ChatReplayRunner(), cases=cases)

    assert outcomes == []
    assert writer.cancelled == ["c-0", "c-1"]
    assert finalize_status(run_kind="case", outcomes=[]) == "no_data"


async def test_pipeline_multiple_batches_do_not_double_count() -> None:
    """run_cases returns only its own batch; the CLI runs replay + pairwise
    sequentially and aggregates both, so earlier batches must not leak in."""
    writer = StubWriter()
    pipeline = EvalPipeline(writer=writer, concurrency=2, content_mode="none")
    pipeline.bind(judge=StubJudge(), generation_service=StubGeneration(), generation_route=object())
    run = _run_row()

    first_cases = [GoldenCase.model_validate(make_roleplay_case(f"a-{i}")) for i in range(3)]
    first = await pipeline.run_cases(run=run, runner=ChatReplayRunner(), cases=first_cases)
    assert len(first) == 3

    base = make_roleplay_case("pw-base")
    pair = PairwiseCase.model_validate(make_pairwise_case(base, case_id="p-0"))
    second = await pipeline.run_cases(run=run, runner=PairwiseReplayRunner(base_cases={"pw-base": GoldenCase.model_validate(base)}), cases=[pair])
    assert len(second) == 1
    assert [o.case_id for o in second] == ["p-0"]

    combined = first + second
    assert len(combined) == 4
    summary = build_case_run_summary(combined)
    assert summary["dimensions"]["persona_fidelity"]["sample_count"] == 3


def test_finalize_status_matrix() -> None:
    def _o(status: str, response: str = "valid") -> Any:
        from src.evaluation.runners.base import CaseOutcome

        outcome = CaseOutcome(
            case_id="x",
            runner="chat_replay",
            status=status,  # type: ignore[arg-type]
            response_status=response,  # type: ignore[arg-type]
            input_ref="case:x",
            output_ref="case:x",
        )
        return outcome

    assert finalize_status(run_kind="case", outcomes=[]) == "no_data"
    assert finalize_status(run_kind="case", outcomes=[_o("completed")]) == "completed"
    assert finalize_status(run_kind="case", outcomes=[_o("completed"), _o("failed", "judge_error")]) == "partial"
    assert finalize_status(run_kind="case", outcomes=[_o("failed", "judge_error")]) == "failed"
    assert finalize_status(run_kind="case", outcomes=[_o("skipped", "skipped")]) == "completed"

    assert finalize_status(run_kind="aggregate", outcomes=[], metric_count=0) == "no_data"
    assert finalize_status(run_kind="aggregate", outcomes=[], metric_count=3) == "completed"
    assert finalize_status(run_kind="aggregate", outcomes=[], metric_count=3, metric_error_count=1) == "partial"
    assert finalize_status(run_kind="aggregate", outcomes=[], metric_count=2, metric_error_count=2) == "failed"


# -- Report -------------------------------------------------------------------


async def test_report_writer_outputs_jsonl_and_markdown(tmp_path: Path) -> None:
    case = GoldenCase.model_validate(make_roleplay_case("rep-1"))
    outcome = await ChatReplayRunner().run_case(
        case, _context(judge=StubJudge(), generation=StubGeneration())
    )
    writer = ReportWriter(out_dir=tmp_path, content_mode="none")
    jsonl_path, md_path = writer.write_run_report(
        run_id="run-1",
        run_metadata={"dataset_id": "parlasoul_gold", "content_mode": "none"},
        outcomes=[outcome],
        summary={"case_total": 1},
    )

    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    meta = json.loads(lines[0])
    assert meta["report"] == "meta"
    result_row = json.loads(lines[2])
    assert result_row["case_id"] == "rep-1"
    # none mode: no raw text anywhere
    assert "A calm, in-character reply." not in jsonl_path.read_text(encoding="utf-8")
    assert md_path.read_text(encoding="utf-8").startswith("# Evaluation Run")


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
