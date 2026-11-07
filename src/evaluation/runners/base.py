"""Runner contract shared by all evaluation runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.evaluation.constants import ContentMode, ResponseStatus, ResultStatus, RunnerName


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CaseExecutionContext:
    """Immutable per-run context handed to every runner.

    ``judge`` is None for aggregate runs (no Judge calls allowed);
    generation stays optional because candidate/pairwise replay may only score
    pre-existing text.
    """

    run_id: UUID
    content_mode: ContentMode
    judge: Any | None = None
    generation_service: Any | None = None
    generation_route: Any | None = None
    generation_timeout_s: float = 120.0
    trace_enabled: bool = False
    # Observability capture for eval LLM calls (Langfuse): default on so
    # Judge prompts/replies are inspectable; flip off to redact.
    capture_input: bool = True
    capture_output: bool = True


@dataclass
class CaseOutcome:
    """One case result, in memory. Raw text may live here transiently;
    persistence applies content_mode rules downstream."""

    case_id: str
    runner: RunnerName
    status: ResultStatus
    response_status: ResponseStatus
    input_ref: str
    output_ref: str
    started_at: datetime = field(default_factory=_utc_now)
    finished_at: datetime | None = None
    duration_ms: int | None = None
    dimension_scores: dict[str, int] = field(default_factory=dict)
    rule_metrics: dict[str, Any] = field(default_factory=dict)
    judge_rationale: str | None = None
    error_code: str | None = None
    error_detail_safe: str | None = None
    input_text: str | None = None
    actual_output_text: str | None = None
    character_id: UUID | None = None
    source_candidate_id: UUID | None = None
    judge_provider: str | None = None
    judge_model: str | None = None
    rubric_version: str | None = None
    prompt_hash: str | None = None
    generation_provider: str | None = None
    generation_model: str | None = None
    generation_model_type: str | None = None
    pairwise_winner: str | None = None
    swap_consistent: bool | None = None
    variant_a_ref: str | None = None
    variant_b_ref: str | None = None
    variant_a_generation_provider: str | None = None
    variant_a_generation_model: str | None = None
    variant_a_generation_model_type: str | None = None
    variant_b_generation_provider: str | None = None
    variant_b_generation_model: str | None = None
    variant_b_generation_model_type: str | None = None

    def finish(self) -> None:
        self.finished_at = _utc_now()
        self.duration_ms = int((self.finished_at - self.started_at).total_seconds() * 1000)


def make_safe_error_detail(template: str, **enum_params: str | int) -> str:
    """Fixed-template safe error detail: enum params only, never raw text."""
    rendered = template.format(**enum_params)
    return rendered[:1000]
