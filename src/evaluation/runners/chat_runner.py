"""ChatReplayRunner: side-effect-free replay generation through the shared seam.

Produces a fresh response per case (no production writes, no growth, no
consolidation), then scores it with the locked Judge route. Raw text only
lives in memory; persistence applies content_mode rules.
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.error_codes import ErrorCode
from src.evaluation.constants import DIMENSIONS
from src.evaluation.datasets.fingerprints import canonical_json
from src.evaluation.datasets.schema import GoldenCase
from src.evaluation.judges.llm_judge import LLMJudge
from src.evaluation.judges.prompts import all_prompt_hashes
from src.evaluation.judges.rule_judge import repetition_3gram_rate
from src.evaluation.runners.base import CaseExecutionContext, CaseOutcome, make_safe_error_detail
from src.evaluation.runners.shared import (
    history_messages_from_case,
    judge_all_dimensions,
    memory_records_from_snapshot,
    output_ref_for_text,
)
from src.services.chat_generation_service import ChatGenerationContext
from src.services.chat_service import _build_user_message_with_images

logger = logging.getLogger(__name__)

OP_REPLAY_GENERATE = "eval.replay.generate"


def prompt_bundle_hash() -> str:
    from src.evaluation.datasets.fingerprints import sha256_text

    return sha256_text(canonical_json(all_prompt_hashes()))


class ChatReplayRunner:
    """Replay golden cases through the shared generation seam + Judge."""

    runner_name = "chat_replay"

    def __init__(self, *, dimensions: tuple[str, ...] = DIMENSIONS) -> None:
        unknown = [d for d in dimensions if d not in DIMENSIONS]
        if unknown:
            raise ValueError(f"unknown dimensions: {unknown}")
        self._dimensions = tuple(dimensions)

    async def run_case(self, case: GoldenCase, context: CaseExecutionContext) -> CaseOutcome:
        outcome = CaseOutcome(
            case_id=case.case_id,
            runner=self.runner_name,
            status="failed",
            response_status="runner_error",
            input_ref=f"case:{case.case_id}",
            output_ref=f"case:{case.case_id}",
            character_id=case.character_id,
            source_candidate_id=case.source_candidate_id,
        )
        try:
            generated_text = await self._generate(case, context, outcome)
        except _GenerationFailed:
            outcome.finish()
            return outcome

        outcome.rule_metrics["repetition_3gram"] = self._repetition_metrics(generated_text)

        judge: LLMJudge | None = context.judge
        if judge is not None:
            await self._judge(case, judge, generated_text, outcome)
        else:
            outcome.status = "completed"
            outcome.response_status = "valid"

        # Raw text stays on the outcome for downstream judging/sanitizing;
        # the pipeline applies content_mode rules before persistence.
        outcome.input_text = case.user_message
        outcome.actual_output_text = generated_text
        if case.task_type == "memory_utility_pair":
            outcome.rule_metrics["metric_kind"] = "memory_utility_pair"
            utility_pair_id = next(
                (
                    tag.split(":", 1)[1]
                    for tag in case.tags
                    if isinstance(tag, str) and tag.startswith("utility_pair:")
                ),
                None,
            )
            if utility_pair_id:
                outcome.rule_metrics["utility_pair_id"] = utility_pair_id

        outcome.finish()
        return outcome

    # -- internals ---------------------------------------------------------

    async def _generate(
        self, case: GoldenCase, context: CaseExecutionContext, outcome: CaseOutcome
    ) -> str:
        memories, id_map = memory_records_from_snapshot(case.memory_snapshot)
        memory_active = case.memory_active if case.memory_active is not None else True
        generation_context = ChatGenerationContext(
            character_system_prompt=str(case.character_snapshot.get("system_prompt", "")),
            history_messages=history_messages_from_case(case),
            user_message=case.user_message,
            memories=memories,
            use_all_semantic=True,
            preferred_expressions=[],
            memory_active=memory_active,
            channel_policy="chat",
        )

        if context.generation_service is None or context.generation_route is None:
            outcome.response_status = "runner_error"
            outcome.error_code = ErrorCode.VALIDATION_FAILED
            outcome.error_detail_safe = make_safe_error_detail(
                "generation_unavailable&stage=replay&reason=not_configured"
            )
            raise _GenerationFailed()

        try:
            result = await context.generation_service.generate_once(
                generation_context,
                route=context.generation_route,
                operation=OP_REPLAY_GENERATE,
                user_message_builder=_build_user_message_with_images,
                retry_policy="none",
                capture_input=context.capture_input,
                capture_output=context.capture_output,
                timeout_s=context.generation_timeout_s,
            )
        except TimeoutError:
            outcome.response_status = "runner_error"
            outcome.error_code = ErrorCode.EXTERNAL_TIMEOUT
            outcome.error_detail_safe = make_safe_error_detail(
                "generation_timeout&stage=replay"
            )
            raise _GenerationFailed() from None
        except Exception as exc:
            outcome.response_status = "runner_error"
            outcome.error_code = ErrorCode.LLM_SERVICE_ERROR
            outcome.error_detail_safe = make_safe_error_detail(
                "generation_error&stage=replay&kind={kind}", kind=type(exc).__name__
            )
            raise _GenerationFailed() from None

        outcome.generation_provider = result.provider
        outcome.generation_model = result.model
        outcome.generation_model_type = result.model_type_label
        outcome.output_ref = output_ref_for_text(result.text)
        outcome.rule_metrics["memory"] = {
            "memory_active": memory_active,
            "injected_episodic_count": len(memories.get("episodic", [])),
            "injected_semantic_count": len(memories.get("semantic", [])),
            "injected_id_map": {str(k): v for k, v in id_map.items()},
        }
        return result.text

    def _repetition_metrics(self, text: str) -> dict[str, Any]:
        result = repetition_3gram_rate(text)
        return {
            "status": result.status,
            "total_3grams": result.total_3grams,
            "repeated_3grams": result.repeated_3grams,
            "rate": result.rate,
        }

    async def _judge(
        self,
        case: GoldenCase,
        judge: LLMJudge,
        response: str,
        outcome: CaseOutcome,
    ) -> None:
        scores, worst, rationale, failed = await judge_all_dimensions(
            judge, case=case, response=response
        )
        outcome.dimension_scores = {
            dimension: score for dimension, score in scores.items() if dimension in self._dimensions
        }
        outcome.judge_provider = judge.provider
        outcome.judge_model = judge.model
        outcome.judge_rationale = rationale
        outcome.prompt_hash = prompt_bundle_hash()

        ooc_outcome = await judge.classify_ooc(
            character_snapshot=case.character_snapshot,
            history_turns=history_messages_from_case(case),
            user_message=case.user_message,
            response=response,
            rubric_context=case.expected_behavior,
        )
        ooc_metrics: dict[str, Any] = {"response_status": ooc_outcome.response_status}
        if ooc_outcome.response_status == "valid":
            ooc_metrics["predicted"] = ooc_outcome.ooc
            if case.ooc_label is not None:
                ooc_metrics["gold"] = case.ooc_label
        else:
            if worst is None:
                worst = ooc_outcome.response_status
        outcome.rule_metrics["ooc"] = ooc_metrics

        if failed or worst is not None:
            outcome.status = "failed"
            outcome.response_status = worst or "judge_error"
            outcome.error_code = ErrorCode.LLM_SERVICE_ERROR
            outcome.error_detail_safe = make_safe_error_detail(
                "judge_failed&stage=score&failed_dimensions={count}",
                count=len(failed),
            )
        else:
            outcome.status = "completed"
            outcome.response_status = "valid"


class _GenerationFailed(Exception):
    """Internal control flow: generation stage already recorded the failure."""
