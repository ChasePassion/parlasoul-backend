"""Evaluation pipeline: concurrency-limited, per-case isolated execution.

Rules:
- generation/judge retries are NOT re-added here (single-attempt seams own
  that); only DB writes are idempotent;
- each case failure is contained and persisted as an explicit status;
- cancellation marks queued/in-flight cases ``cancelled`` without touching
  completed rows;
- content_mode rules are applied at the persistence boundary; sanitizer
  violations are fail-closed per row.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.core.error_codes import ErrorCode
from src.db.models import EvalResult, EvalRun
from src.evaluation.constants import RUNNERS
from src.evaluation.metrics.sanitize import (
    SanitizerViolationError,
    sanitize_rationale,
    sanitize_value,
)
from src.evaluation.runners.base import CaseOutcome, make_safe_error_detail
from src.evaluation.storage.db_writer import DbWriter

logger = logging.getLogger(__name__)


@dataclass
class PipelineState:
    cancelled: bool = False
    outcomes: list[CaseOutcome] = field(default_factory=list)


def outcome_to_eval_result(
    outcome: CaseOutcome,
    *,
    run: EvalRun,
    content_mode: str,
) -> EvalResult:
    """Apply content_mode + sanitizer at the persistence boundary."""
    rationale: str | None
    if content_mode == "none":
        from src.evaluation.runners.shared import rationale_hash

        hashed = rationale_hash(outcome.judge_rationale)
        rationale = hashed
    else:
        rationale = sanitize_rationale(outcome.judge_rationale, content_mode=content_mode)

    dimension_scores = sanitize_value(outcome.dimension_scores, content_mode=content_mode)
    rule_metrics = sanitize_value(outcome.rule_metrics, content_mode=content_mode)

    keep_text = content_mode in ("redacted", "full")
    return EvalResult(
        run_id=run.id,
        case_id=outcome.case_id,
        runner=outcome.runner,
        source_candidate_id=outcome.source_candidate_id,
        character_id=outcome.character_id,
        owner_user_id=run.owner_user_id,
        status=outcome.status,
        response_status=outcome.response_status,
        started_at=outcome.started_at,
        finished_at=outcome.finished_at,
        duration_ms=outcome.duration_ms,
        content_mode=content_mode,
        input_ref=outcome.input_ref,
        output_ref=outcome.output_ref,
        input_text=outcome.input_text if keep_text else None,
        actual_output_text=outcome.actual_output_text if keep_text else None,
        dimension_scores_json=dimension_scores,
        rule_metrics_json=rule_metrics,
        judge_rationale=rationale[:4000] if rationale else None,
        judge_provider=outcome.judge_provider,
        judge_model=outcome.judge_model,
        rubric_version=outcome.rubric_version or run.rubric_version,
        prompt_hash=outcome.prompt_hash or run.prompt_hash,
        generation_provider=outcome.generation_provider,
        generation_model=outcome.generation_model,
        generation_model_type=outcome.generation_model_type,
        pairwise_winner=outcome.pairwise_winner,
        swap_consistent=outcome.swap_consistent,
        variant_a_ref=outcome.variant_a_ref,
        variant_b_ref=outcome.variant_b_ref,
        variant_a_generation_provider=outcome.variant_a_generation_provider,
        variant_a_generation_model=outcome.variant_a_generation_model,
        variant_a_generation_model_type=outcome.variant_a_generation_model_type,
        variant_b_generation_provider=outcome.variant_b_generation_provider,
        variant_b_generation_model=outcome.variant_b_generation_model,
        variant_b_generation_model_type=outcome.variant_b_generation_model_type,
        error_code=outcome.error_code,
        error_detail_safe=(outcome.error_detail_safe or None)
        and outcome.error_detail_safe[:1000],
    )


class EvalPipeline:
    def __init__(
        self,
        *,
        writer: DbWriter,
        concurrency: int,
        content_mode: str,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency must be positive")
        self._writer = writer
        self._concurrency = concurrency
        self._content_mode = content_mode
        self._state = PipelineState()
        self._lock = asyncio.Lock()

    def request_cancel(self) -> None:
        self._state.cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._state.cancelled

    async def run_cases(
        self,
        *,
        run: EvalRun,
        runner: Any,
        cases: list[Any],
    ) -> list[CaseOutcome]:
        """Execute all cases; returns outcomes (completed + failed + skipped).

        Cancelled cases are persisted by the caller-visible writer path
        before this returns.
        """
        if runner.runner_name not in RUNNERS:
            raise ValueError(f"unknown runner: {runner.runner_name}")

        semaphore = asyncio.Semaphore(self._concurrency)
        pending_case_ids: list[str] = []
        tasks: list[asyncio.Task] = []

        for case in cases:
            case_id = _case_id(case)
            pending_case_ids.append(case_id)
            tasks.append(
                asyncio.create_task(
                    self._run_one(run=run, runner=runner, case=case, case_id=case_id, semaphore=semaphore),
                    name=f"eval.case.{case_id}",
                )
            )

        try:
            batch_results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            self._state.cancelled = True
            raise

        if self._state.cancelled:
            completed_ids = {outcome.case_id for outcome in self._state.outcomes}
            cancelled_ids = [
                case_id for case_id in pending_case_ids if case_id not in completed_ids
            ]
            if cancelled_ids:
                await self._writer.mark_cases_cancelled(
                    run_id=run.id,
                    runner=runner.runner_name,
                    case_ids=cancelled_ids,
                    content_mode=self._content_mode,
                    owner_user_id=run.owner_user_id,
                )
        # Only this batch's outcomes — callers may invoke run_cases multiple
        # times (replay + pairwise) and must not double-count earlier batches.
        return [outcome for outcome in batch_results if outcome is not None]

    async def _run_one(
        self,
        *,
        run: EvalRun,
        runner: Any,
        case: Any,
        case_id: str,
        semaphore: asyncio.Semaphore,
    ) -> CaseOutcome | None:
        async with semaphore:
            if self._state.cancelled:
                return None
            context = self._build_context(run)
            outcome = await self._execute_safely(runner, case, case_id, context)
            await self._persist(run, outcome)
            async with self._lock:
                self._state.outcomes.append(outcome)
            return outcome

    def _build_context(self, run: EvalRun) -> Any:
        from src.evaluation.runners.base import CaseExecutionContext

        return CaseExecutionContext(
            run_id=run.id,
            content_mode=self._content_mode,  # type: ignore[arg-type]
            judge=getattr(self, "_judge", None),
            generation_service=getattr(self, "_generation_service", None),
            generation_route=getattr(self, "_generation_route", None),
            generation_timeout_s=getattr(self, "_generation_timeout_s", 120.0),
            capture_input=getattr(self, "_capture_input", True),
            capture_output=getattr(self, "_capture_output", True),
        )

    def bind(
        self,
        *,
        judge: Any | None = None,
        generation_service: Any | None = None,
        generation_route: Any | None = None,
        generation_timeout_s: float = 120.0,
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> None:
        self._judge = judge
        self._generation_service = generation_service
        self._generation_route = generation_route
        self._generation_timeout_s = generation_timeout_s
        self._capture_input = capture_input
        self._capture_output = capture_output

    async def _execute_safely(
        self, runner: Any, case: Any, case_id: str, context: Any
    ) -> CaseOutcome:
        try:
            outcome = await runner.run_case(case, context)
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Evaluation case failed unexpectedly: case_id=%s", case_id)
            outcome = CaseOutcome(
                case_id=case_id,
                runner=runner.runner_name,
                status="failed",
                response_status="runner_error",
                input_ref=f"case:{case_id}",
                output_ref=f"case:{case_id}",
                error_code=ErrorCode.INTERNAL_ERROR,
                error_detail_safe=make_safe_error_detail(
                    "runner_exception&kind={kind}", kind=type(exc).__name__
                ),
            )
            outcome.finish()
            return outcome

    async def _persist(self, run: EvalRun, outcome: CaseOutcome) -> None:
        try:
            result = outcome_to_eval_result(outcome, run=run, content_mode=self._content_mode)
        except SanitizerViolationError:
            # Fail-closed: never persist a row that may carry raw text.
            outcome.status = "failed"
            outcome.response_status = "runner_error"
            outcome.error_code = ErrorCode.VALIDATION_FAILED
            outcome.error_detail_safe = make_safe_error_detail(
                "sanitizer_violation&stage=persist"
            )
            outcome.dimension_scores = {}
            outcome.rule_metrics = {}
            outcome.judge_rationale = None
            outcome.input_text = None
            outcome.actual_output_text = None
            result = outcome_to_eval_result(outcome, run=run, content_mode=self._content_mode)
        await self._writer.upsert_result(result)


def _case_id(case: Any) -> str:
    case_id = getattr(case, "case_id", None) or getattr(case, "scenario_id", None)
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case without case_id/scenario_id cannot enter the pipeline")
    return case_id


def finalize_status(
    *,
    run_kind: str,
    outcomes: list[CaseOutcome],
    metric_count: int = 0,
    metric_error_count: int = 0,
) -> str:
    """Compute the terminal run status from outcome counts (plan §2.5)."""
    if run_kind == "aggregate":
        if metric_count == 0:
            return "no_data"
        if metric_error_count == 0:
            return "completed"
        if metric_error_count >= metric_count:
            return "failed"
        return "partial"

    if not outcomes:
        return "no_data"
    non_cancelled = [o for o in outcomes if o.status != "cancelled"]
    completed = sum(1 for o in non_cancelled if o.status == "completed")
    failed = sum(1 for o in non_cancelled if o.status == "failed")
    acceptable = sum(1 for o in non_cancelled if o.status in ("skipped", "no_data"))
    if failed == 0 and (completed > 0 or acceptable == len(non_cancelled)):
        return "completed"
    if completed == 0 and failed > 0 and acceptable == 0:
        return "failed"
    if completed == 0 and failed == 0:
        return "no_data"
    return "partial"


def utc_now() -> datetime:
    return datetime.now(UTC)
