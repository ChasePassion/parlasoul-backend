"""Versioned, fail-closed LLM Judge.

Guarantees per phase18 contract:
- one locked route (injected, resolved before run start);
- single upstream attempt (retry_policy="none"), per-call total timeout at
  this boundary;
- strict Pydantic validation of Judge JSON — timeouts, network errors,
  cancellations and parse failures are explicit response states, never
  default scores;
- observability capture (Langfuse input/output) defaults ON so Judge
  prompts/replies stay inspectable; flip the capture flags to redact.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.error_codes import ErrorCode
from src.core.llm_registry import ResolvedLLMRoute
from src.evaluation.constants import ResponseStatus
from src.evaluation.judges.prompts import (
    abstention_system_prompt,
    build_judge_user_message,
    build_pairwise_user_message,
    claims_system_prompt,
    dimension_rubric,
    ooc_system_prompt,
    pairwise_system_prompt,
    prompt_hash,
)
from src.evaluation.judges.routes import JUDGE_ROUTE_SOURCE, ResolvedJudgeRoute
from src.services.llm_gateway_service import LLMGatewayService

OP_SCORE = "eval.judge.score"
OP_OOC = "eval.judge.ooc"
OP_COMPARE = "eval.judge.compare"
OP_CLAIMS = "eval.judge.claims"
OP_ABSTENTION = "eval.judge.abstention"
ROUTE_ROLE = "evaluation"


class DimensionScoreResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    score: int = Field(ge=1, le=5)
    rationale: str = Field(default="", max_length=4000)


class OOCResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ooc: bool
    rationale: str = Field(default="", max_length=4000)


class PairwiseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    winner: Literal["response_1", "response_2", "tie"]
    rationale: str = Field(default="", max_length=4000)


class ClaimsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    supported_claims: list[str] = Field(default_factory=list)
    rationale: str = Field(default="", max_length=4000)


class AbstentionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    abstained: bool
    rationale: str = Field(default="", max_length=4000)


@dataclass(frozen=True)
class JudgeCallOutcome:
    """One Judge call result. ``raw_text`` stays in memory only; persistence
    is governed by content_mode and the sanitizer downstream."""

    response_status: ResponseStatus
    raw_text: str | None
    score: int | None
    ooc: bool | None
    winner_position: str | None
    rationale: str | None
    error_code: str | None
    duration_ms: int
    upstream_attempts: int = 1
    supported_claims: tuple[str, ...] = ()
    abstained: bool | None = None


class LLMJudge:
    def __init__(
        self,
        *,
        route: ResolvedJudgeRoute,
        timeout_s: float,
        gateway_service: LLMGatewayService | None = None,
        rubric_version: str,
        prompt_template_version: str,
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> None:
        self._route = route
        self._timeout_s = timeout_s
        self._gateway = gateway_service or LLMGatewayService()
        self.rubric_version = rubric_version
        self.prompt_template_version = prompt_template_version
        self._capture_input = capture_input
        self._capture_output = capture_output

    @property
    def route(self) -> ResolvedJudgeRoute:
        return self._route

    @property
    def provider(self) -> str:
        return self._route.provider

    @property
    def model(self) -> str:
        return self._route.model

    def prompt_hash_for(self, kind: str) -> str:
        if kind == "ooc":
            return prompt_hash(ooc_system_prompt())
        if kind == "pairwise":
            return prompt_hash(pairwise_system_prompt())
        return prompt_hash(dimension_rubric(kind))

    # -- public operations ------------------------------------------------

    async def score_dimension(
        self,
        *,
        dimension: str,
        character_snapshot: dict[str, Any],
        history_turns: list[dict[str, Any]],
        user_message: str,
        response: str,
        rubric_context: str | None = None,
    ) -> JudgeCallOutcome:
        return await self._call(
            operation=OP_SCORE,
            system_prompt=f"{dimension_rubric(dimension)}",
            user_message=build_judge_user_message(
                character_snapshot=character_snapshot,
                history_turns=history_turns,
                user_message=user_message,
                response=response,
                rubric_context=rubric_context,
            ),
            response_model=DimensionScoreResponse,
        )

    async def classify_ooc(
        self,
        *,
        character_snapshot: dict[str, Any],
        history_turns: list[dict[str, Any]],
        user_message: str,
        response: str,
        rubric_context: str | None = None,
    ) -> JudgeCallOutcome:
        return await self._call(
            operation=OP_OOC,
            system_prompt=ooc_system_prompt(),
            user_message=build_judge_user_message(
                character_snapshot=character_snapshot,
                history_turns=history_turns,
                user_message=user_message,
                response=response,
                rubric_context=rubric_context,
            ),
            response_model=OOCResponse,
        )

    async def compare(
        self,
        *,
        character_snapshot: dict[str, Any],
        history_turns: list[dict[str, Any]],
        user_message: str,
        response_1: str,
        response_2: str,
    ) -> JudgeCallOutcome:
        return await self._call(
            operation=OP_COMPARE,
            system_prompt=pairwise_system_prompt(),
            user_message=build_pairwise_user_message(
                character_snapshot=character_snapshot,
                history_turns=history_turns,
                user_message=user_message,
                response_1=response_1,
                response_2=response_2,
            ),
            response_model=PairwiseResponse,
        )

    async def judge_claims(
        self,
        *,
        claims: list[dict[str, Any]],
        response: str,
        memories: list[dict[str, Any]] | None = None,
    ) -> JudgeCallOutcome:
        payload = {
            "claims": [
                {"claim_id": c.get("claim_id"), "text": c.get("text")} for c in claims
            ],
            "response": response,
        }
        if memories:
            payload["memories"] = memories
        return await self._call(
            operation=OP_CLAIMS,
            system_prompt=claims_system_prompt(),
            user_message=json.dumps(payload, ensure_ascii=False),
            response_model=ClaimsResponse,
        )

    async def judge_abstention(
        self,
        *,
        character_snapshot: dict[str, Any],
        history_turns: list[dict[str, Any]],
        user_message: str,
        response: str,
    ) -> JudgeCallOutcome:
        return await self._call(
            operation=OP_ABSTENTION,
            system_prompt=abstention_system_prompt(),
            user_message=build_judge_user_message(
                character_snapshot=character_snapshot,
                history_turns=history_turns,
                user_message=user_message,
                response=response,
            ),
            response_model=AbstentionResponse,
        )

    # -- internals ----------------------------------------------------------

    def _gateway_route(self) -> ResolvedLLMRoute:
        return ResolvedLLMRoute(
            provider=self._route.provider,
            model=self._route.model,
            sdk_kind=self._route.sdk_kind,  # type: ignore[arg-type]
            api_key=self._route.api_key,
            api_base=self._route.api_base,
            source=JUDGE_ROUTE_SOURCE,
            model_type_label=self._route.model_type_label,
        )

    async def _call(
        self,
        *,
        operation: str,
        system_prompt: str,
        user_message: str,
        response_model: type[BaseModel],
    ) -> JudgeCallOutcome:
        started = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                self._gateway.generate_text(
                    route=self._gateway_route(),
                    system_prompt=system_prompt,
                    user_message=user_message,
                    operation=operation,
                    route_role=ROUTE_ROLE,
                    retry_policy="none",
                    capture_input=self._capture_input,
                    capture_output=self._capture_output,
                ),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            return self._outcome(
                status="judge_timeout",
                raw_text=None,
                error_code=ErrorCode.EXTERNAL_TIMEOUT,
                started=started,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._outcome(
                status="judge_error",
                raw_text=None,
                error_code=ErrorCode.LLM_SERVICE_ERROR,
                started=started,
            )

        parsed = self._parse(raw, response_model)
        if parsed is None:
            return self._outcome(
                status="judge_parse_failed",
                raw_text=raw,
                error_code=ErrorCode.VALIDATION_FAILED,
                started=started,
            )

        score = getattr(parsed, "score", None)
        ooc = getattr(parsed, "ooc", None)
        winner = getattr(parsed, "winner", None)
        supported_claims = tuple(getattr(parsed, "supported_claims", ()) or ())
        abstained = getattr(parsed, "abstained", None)
        return self._outcome(
            status="valid",
            raw_text=raw,
            error_code=None,
            started=started,
            score=score,
            ooc=ooc,
            winner_position=winner,
            rationale=getattr(parsed, "rationale", None),
            supported_claims=supported_claims,
            abstained=abstained,
        )

    @staticmethod
    def _parse(raw: str | None, response_model: type[BaseModel]) -> BaseModel | None:
        if not raw or not raw.strip():
            return None
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            return response_model.model_validate(data)
        except ValidationError:
            return None

    @staticmethod
    def _outcome(
        *,
        status: ResponseStatus,
        raw_text: str | None,
        error_code: str | None,
        started: float,
        score: int | None = None,
        ooc: bool | None = None,
        winner_position: str | None = None,
        rationale: str | None = None,
        supported_claims: tuple[str, ...] = (),
        abstained: bool | None = None,
    ) -> JudgeCallOutcome:
        return JudgeCallOutcome(
            response_status=status,
            raw_text=raw_text,
            score=score,
            ooc=ooc,
            winner_position=winner_position,
            rationale=rationale,
            error_code=error_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            supported_claims=supported_claims,
            abstained=abstained,
        )
