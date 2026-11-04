"""MemoryReplayRunner: memory task cases with snapshot injection and traces.

Faithfulness / abstention / OOC are generation-side judgments and are computed
here. Retrieval-dependent metrics (Recall/Precision/Conflict/Update) need a
live retrieval provenance trace; without one they are reported
``hook_required`` — never inferred from reply text.
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.error_codes import ErrorCode
from src.evaluation.datasets.schema import GoldenCase
from src.evaluation.runners.base import CaseExecutionContext, CaseOutcome, make_safe_error_detail
from src.evaluation.runners.chat_runner import ChatReplayRunner
from src.evaluation.runners.shared import history_messages_from_case
from src.memory_system.processors.memory_usage_judge import MemoryUsageJudge, MemoryUsageTrace
from src.services.llm_gateway_service import LLMGatewayService

logger = logging.getLogger(__name__)

_RETRIEVAL_DEPENDENT_TASKS = frozenset(
    {"memory_recall", "memory_precision", "memory_update", "memory_temporal"}
)


class MemoryReplayRunner(ChatReplayRunner):
    """Extends chat replay with memory-specific judgments and usage traces."""

    runner_name = "chat_replay"  # plan: memory outcomes persist as chat_replay rows

    def __init__(
        self,
        *,
        gateway_service: LLMGatewayService | None = None,
        judge_route: Any | None = None,
    ) -> None:
        super().__init__()
        self._gateway_service = gateway_service
        self._judge_route = judge_route

    async def run_case(self, case: GoldenCase, context: CaseExecutionContext) -> CaseOutcome:
        outcome = await super().run_case(case, context)
        if outcome.response_status != "valid":
            # Generation/judge already failed; no memory post-processing.
            return outcome
        outcome.rule_metrics.setdefault("metric_kind", case.task_type)

        generated_text = outcome.actual_output_text or ""

        if case.task_type in _RETRIEVAL_DEPENDENT_TASKS:
            outcome.rule_metrics["retrieval_dependent"] = {"status": "hook_required"}

        if case.task_type == "memory_faithfulness" and context.judge is not None:
            await self._judge_faithfulness(case, context, generated_text, outcome)

        if case.task_type == "memory_abstention" and context.judge is not None:
            await self._judge_abstention(case, context, generated_text, outcome)

        if case.task_type == "memory_ooc" and context.judge is not None:
            # Base chat replay already ran classify_ooc into rule_metrics["ooc"].
            pass

        await self._record_memory_usage(case, context, generated_text, outcome)
        return outcome

    # -- internals ---------------------------------------------------------

    async def _judge_faithfulness(
        self,
        case: GoldenCase,
        context: CaseExecutionContext,
        response: str,
        outcome: CaseOutcome,
    ) -> None:
        claims_payload = [
            {"claim_id": claim.get("claim_id"), "text": claim.get("text")}
            for claim in case.expected_claims
            if isinstance(claim, dict)
        ]
        memories_payload = (
            [
                {"memory_id": item.memory_id, "text": item.text}
                for bucket in ("episodic", "semantic")
                for item in getattr(case.memory_snapshot, bucket)
            ]
            if case.memory_snapshot is not None
            else []
        )
        call = await context.judge.judge_claims(
            claims=claims_payload,
            response=response,
            memories=memories_payload,
        )
        if call.response_status != "valid":
            self._degrade(outcome, call.response_status, "claims")
            outcome.rule_metrics["faithfulness"] = {"response_status": call.response_status}
            return

        claim_ids = {str(claim.get("claim_id")) for claim in claims_payload}
        supported = [cid for cid in call.supported_claims if cid in claim_ids]
        required = [
            str(claim.get("claim_id"))
            for claim in case.expected_claims
            if isinstance(claim, dict) and claim.get("is_required")
        ]
        supported_required = [cid for cid in supported if cid in set(required)]
        outcome.rule_metrics["faithfulness"] = {
            "response_status": "valid",
            "supported_claims": supported,
            "required_claims": required,
            "supported_required_count": len(supported_required),
            "required_count": len(required),
            "rate": (len(supported_required) / len(required)) if required else None,
        }
        if call.rationale:
            outcome.judge_rationale = _merge_rationale(outcome.judge_rationale, call.rationale)

    async def _judge_abstention(
        self,
        case: GoldenCase,
        context: CaseExecutionContext,
        response: str,
        outcome: CaseOutcome,
    ) -> None:
        call = await context.judge.judge_abstention(
            character_snapshot=case.character_snapshot,
            history_turns=history_messages_from_case(case),
            user_message=case.user_message,
            response=response,
        )
        if call.response_status != "valid" or call.abstained is None:
            self._degrade(outcome, call.response_status, "abstention")
            outcome.rule_metrics["abstention"] = {"response_status": call.response_status}
            return
        outcome.rule_metrics["abstention"] = {
            "response_status": "valid",
            "predicted": call.abstained,
            "gold": case.should_abstain,
            "correct": (call.abstained == case.should_abstain)
            if case.should_abstain is not None
            else None,
        }
        if call.rationale:
            outcome.judge_rationale = _merge_rationale(outcome.judge_rationale, call.rationale)

    async def _record_memory_usage(
        self,
        case: GoldenCase,
        context: CaseExecutionContext,
        response: str,
        outcome: CaseOutcome,
    ) -> None:
        snapshot = case.memory_snapshot
        if snapshot is None or not snapshot.episodic:
            return
        if self._gateway_service is None or self._judge_route is None:
            outcome.rule_metrics["memory_usage"] = {"judge_status": "unavailable"}
            return

        judge = MemoryUsageJudge(self._gateway_service, self._judge_route)
        episodic_input = [
            {"id": index + 1, "text": item.text}
            for index, item in enumerate(snapshot.episodic)
        ]
        captured: list[MemoryUsageTrace] = []
        used_ids = await judge.judge_used_memory_ids(
            episodic_memories=episodic_input,
            last_user=case.user_message,
            last_assistant=response,
            user_id="evaluation",
            chat_id=None,
            character_id=str(case.character_ref or ""),
            trace_sink=captured.append,
        )
        trace = captured[0] if captured else None
        id_map = {index + 1: item.memory_id for index, item in enumerate(snapshot.episodic)}
        outcome.rule_metrics["memory_usage"] = {
            "judge_status": trace.judge_status if trace else "unavailable",
            "retrieved_ids": [id_map[i] for i in (trace.retrieved_ids if trace else ())],
            "used_ids": [id_map[i] for i in used_ids if i in id_map],
            "prompt_hash": trace.prompt_hash if trace else None,
            "duration_ms": trace.duration_ms if trace else None,
        }

    @staticmethod
    def _degrade(outcome: CaseOutcome, response_status: str, stage: str) -> None:
        outcome.status = "failed"
        outcome.response_status = response_status  # type: ignore[assignment]
        outcome.error_code = ErrorCode.LLM_SERVICE_ERROR
        outcome.error_detail_safe = make_safe_error_detail(
            "memory_judge_failed&stage={stage}", stage=stage
        )


def _merge_rationale(existing: str | None, addition: str) -> str:
    if not existing:
        return addition
    return f"{existing}\n{addition}"
