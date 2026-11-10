"""Aggregation contracts: explicit denominators, no default scores."""

from __future__ import annotations

from src.evaluation.metrics.aggregate import (
    aggregate_dimensions,
)
from src.evaluation.runners.base import CaseOutcome


def _outcome(
    case_id: str,
    *,
    runner: str = "chat_replay",
    status: str = "completed",
    response_status: str = "valid",
    dimension_scores: dict | None = None,
    rule_metrics: dict | None = None,
    pairwise_winner: str | None = None,
    swap_consistent: bool | None = None,
    variant_a_model: str = "model-a",
    variant_b_model: str = "model-b",
) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        runner=runner,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        response_status=response_status,  # type: ignore[arg-type]
        input_ref=f"case:{case_id}",
        output_ref=f"case:{case_id}",
        dimension_scores=dimension_scores or {},
        rule_metrics=rule_metrics or {},
        pairwise_winner=pairwise_winner,
        swap_consistent=swap_consistent,
        variant_a_generation_model=variant_a_model if runner == "pairwise_replay" else None,
        variant_b_generation_model=variant_b_model if runner == "pairwise_replay" else None,
    )


def test_dimension_aggregation_valid_cases_only() -> None:
    outcomes = [
        _outcome("c1", dimension_scores={"persona_fidelity": 5}),
        _outcome("c2", dimension_scores={"persona_fidelity": 3}),
        _outcome("c3", response_status="judge_parse_failed", dimension_scores={"persona_fidelity": 1}),
    ]
    result = aggregate_dimensions(outcomes)
    persona = result["persona_fidelity"]
    assert persona["status"] == "available"
    assert persona["sample_count"] == 2
    assert persona["mean"] == 4.0
    assert result["multi_turn_consistency"]["status"] == "no_data"


