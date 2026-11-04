"""CandidateReplayRunner: offline scoring of pre-existing production candidates.

Loads the candidate (plus its character and branch history) through the
system-context eval connection, filters greeting/failed/non-final rows, then
scores the stored content with the locked Judge route. No generation happens.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.core.error_codes import ErrorCode
from src.db.models import Candidate, Character, Turn
from src.db.session import AsyncSessionLocal
from src.evaluation.constants import DIMENSIONS
from src.evaluation.datasets.schema import GoldenCase
from src.evaluation.runners.base import CaseExecutionContext, CaseOutcome, make_safe_error_detail
from src.services.chat_service import _load_llm_history_messages_from_leaf

logger = logging.getLogger(__name__)

OP_CANDIDATE_SCORE = "eval.candidate.score"


class CandidateReplayRunner:
    runner_name = "candidate_replay"

    def __init__(self, *, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory or AsyncSessionLocal

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

        if case.source_candidate_id is None:
            self._skip(outcome, "no_source_candidate")
            return outcome
        outcome.output_ref = str(case.source_candidate_id)

        loaded = await self._load_candidate_context(case)
        if loaded is None:
            self._skip(outcome, "candidate_not_found")
            return outcome
        candidate, character, history_messages = loaded

        extra = candidate.extra or {}
        if not candidate.is_final:
            self._skip(outcome, "candidate_not_final")
            return outcome
        if extra.get("error_code"):
            self._skip(outcome, "candidate_failed_generation")
            return outcome
        if extra.get("source") == "greeting":
            self._skip(outcome, "candidate_static_greeting")
            return outcome
        content = (candidate.content or "").strip()
        if not content:
            self._skip(outcome, "candidate_empty_content")
            return outcome

        if context.judge is None:
            outcome.error_code = ErrorCode.VALIDATION_FAILED
            outcome.error_detail_safe = make_safe_error_detail(
                "judge_unavailable&stage=candidate_replay"
            )
            outcome.finish()
            return outcome

        character_snapshot = self._character_snapshot(case, character)
        outcome.generation_model_type = candidate.model_type

        worst: str | None = None
        rationale_parts: list[str] = []
        for dimension in DIMENSIONS:
            call = await context.judge.score_dimension(
                dimension=dimension,
                character_snapshot=character_snapshot,
                history_turns=history_messages,
                user_message=self._parent_user_text(history_messages),
                response=content,
            )
            if call.response_status == "valid" and call.score is not None:
                outcome.dimension_scores[dimension] = call.score
                if call.rationale:
                    rationale_parts.append(f"[{dimension}] {call.rationale}")
            elif worst is None:
                worst = call.response_status

        ooc_call = await context.judge.classify_ooc(
            character_snapshot=character_snapshot,
            history_turns=history_messages,
            user_message=self._parent_user_text(history_messages),
            response=content,
        )
        ooc_metrics: dict[str, Any] = {"response_status": ooc_call.response_status}
        if ooc_call.response_status == "valid":
            ooc_metrics["predicted"] = ooc_call.ooc
            if case.ooc_label is not None:
                ooc_metrics["gold"] = case.ooc_label
        elif worst is None:
            worst = ooc_call.response_status
        outcome.rule_metrics["ooc"] = ooc_metrics

        outcome.judge_provider = context.judge.provider
        outcome.judge_model = context.judge.model
        outcome.judge_rationale = "\n".join(rationale_parts) if rationale_parts else None

        if worst is not None or len(outcome.dimension_scores) < len(DIMENSIONS):
            outcome.status = "failed"
            outcome.response_status = worst or "judge_error"
            outcome.error_code = ErrorCode.LLM_SERVICE_ERROR
            outcome.error_detail_safe = make_safe_error_detail(
                "candidate_judge_failed&scored={count}", count=len(outcome.dimension_scores)
            )
        else:
            outcome.status = "completed"
            outcome.response_status = "valid"

        outcome.finish()
        return outcome

    # -- internals ---------------------------------------------------------

    def _skip(self, outcome: CaseOutcome, reason: str) -> None:
        outcome.status = "skipped"
        outcome.response_status = "skipped"
        outcome.error_code = ErrorCode.VALIDATION_FAILED
        outcome.error_detail_safe = make_safe_error_detail(
            "candidate_skipped&reason={reason}", reason=reason
        )
        outcome.finish()

    async def _load_candidate_context(
        self, case: GoldenCase
    ) -> tuple[Candidate, Character | None, list[dict[str, Any]]] | None:
        async with self._session_factory() as session:
            session.info["current_user_id"] = "system"
            candidate = await session.get(Candidate, case.source_candidate_id)
            if candidate is None:
                return None

            turn: Turn | None = None
            character: Character | None = None
            if candidate.turn_id is not None:
                turn = await session.get(Turn, candidate.turn_id)
            if turn is not None and turn.author_character_id is not None:
                character = await session.get(Character, turn.author_character_id)

            history_messages: list[dict[str, Any]] = []
            if turn is not None and turn.parent_turn_id is not None:
                from src.repositories.candidate_repository import CandidateRepository
                from src.repositories.turn_repository import TurnRepository

                history_messages = await _load_llm_history_messages_from_leaf(
                    turn_repo=TurnRepository(session),
                    candidate_repo=CandidateRepository(session),
                    chat_id=turn.chat_id,
                    leaf_turn_id=turn.parent_turn_id,
                    limit=20,
                )
            return candidate, character, history_messages

    @staticmethod
    def _character_snapshot(case: GoldenCase, character: Character | None) -> dict[str, Any]:
        if case.character_snapshot and case.character_snapshot.get("system_prompt"):
            return case.character_snapshot
        if character is None:
            return {"name": "", "description": "", "system_prompt": ""}
        return {
            "name": character.name or "",
            "description": getattr(character, "description", "") or "",
            "system_prompt": character.system_prompt or "",
        }

    @staticmethod
    def _parent_user_text(history_messages: list[dict[str, Any]]) -> str:
        for message in reversed(history_messages):
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [
                        str(part.get("text", ""))
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ]
                    return "\n".join(part for part in parts if part)
        return ""
