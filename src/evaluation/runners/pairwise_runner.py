"""PairwiseReplayRunner: blind A/B comparison with mandatory order swap.

Consumes two already-produced variants (inline dataset content in phase18);
never generates, never sees variant identity in the Judge prompt.
"""

from __future__ import annotations

import logging

from src.core.error_codes import ErrorCode
from src.evaluation.datasets.schema import GoldenCase, PairwiseCase
from src.evaluation.judges.pairwise import judge_pairwise_with_swap
from src.evaluation.runners.base import CaseExecutionContext, CaseOutcome, make_safe_error_detail

logger = logging.getLogger(__name__)


class PairwiseReplayRunner:
    runner_name = "pairwise_replay"

    def __init__(self, *, base_cases: dict[str, GoldenCase] | None = None) -> None:
        self._base_cases = base_cases or {}

    async def run_case(self, case: PairwiseCase, context: CaseExecutionContext) -> CaseOutcome:
        outcome = CaseOutcome(
            case_id=case.case_id,
            runner=self.runner_name,
            status="failed",
            response_status="runner_error",
            input_ref=f"case:{case.case_id}",
            output_ref=case.base_context_fingerprint,
            variant_a_ref=case.variant_a.variant_ref,
            variant_b_ref=case.variant_b.variant_ref,
            variant_a_generation_provider=case.variant_a.generation_provider,
            variant_a_generation_model=case.variant_a.generation_model,
            variant_a_generation_model_type=case.variant_a.generation_model_type,
            variant_b_generation_provider=case.variant_b.generation_provider,
            variant_b_generation_model=case.variant_b.generation_model,
            variant_b_generation_model_type=case.variant_b.generation_model_type,
        )

        if context.judge is None:
            outcome.error_code = ErrorCode.VALIDATION_FAILED
            outcome.error_detail_safe = make_safe_error_detail(
                "judge_unavailable&stage=pairwise&reason=not_configured"
            )
            outcome.finish()
            return outcome

        variant_a_content = case.variant_a.content
        variant_b_content = case.variant_b.content
        if variant_a_content is None or variant_b_content is None:
            # Production sidecars are not resolved by the phase18 loader; this
            # is fail-closed, never a silent skip.
            outcome.response_status = "no_data"
            outcome.status = "no_data"
            outcome.error_code = ErrorCode.VALIDATION_FAILED
            outcome.error_detail_safe = make_safe_error_detail(
                "pairwise_variant_content_missing&stage=pairwise"
            )
            outcome.finish()
            return outcome

        base_snapshot = self._base_cases.get(case.base_case_id)
        if base_snapshot is None:
            outcome.response_status = "runner_error"
            outcome.error_code = ErrorCode.VALIDATION_FAILED
            outcome.error_detail_safe = make_safe_error_detail(
                "pairwise_base_unresolved&stage=pairwise"
            )
            outcome.finish()
            return outcome

        result = await judge_pairwise_with_swap(
            context.judge,
            character_snapshot=base_snapshot.character_snapshot,
            history_turns=[
                {"role": turn.role, "content": turn.content}
                for turn in base_snapshot.history_turns
            ],
            user_message=base_snapshot.user_message,
            variant_a_content=variant_a_content,
            variant_b_content=variant_b_content,
        )

        outcome.judge_provider = context.judge.provider
        outcome.judge_model = context.judge.model
        outcome.response_status = result.response_status
        outcome.pairwise_winner = result.winner
        outcome.swap_consistent = result.swap_consistent
        outcome.rule_metrics["pairwise"] = {
            "first_status": result.first.response_status,
            "second_status": result.second.response_status if result.second else None,
            "upstream_attempts": result.first.upstream_attempts,
        }
        if result.response_status == "valid":
            outcome.status = "completed"
            outcome.judge_rationale = result.first.rationale
        else:
            outcome.status = "failed"
            outcome.error_code = (
                result.error_code or ErrorCode.LLM_SERVICE_ERROR
            )
            outcome.error_detail_safe = make_safe_error_detail(
                "pairwise_judge_failed&status={status}", status=result.response_status
            )
        outcome.finish()
        return outcome
