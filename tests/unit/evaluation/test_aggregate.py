"""Aggregation contracts: explicit denominators, no default scores."""

from __future__ import annotations

from src.evaluation.metrics.aggregate import (
    aggregate_dimensions,
    aggregate_memory,
    aggregate_ooc,
    aggregate_pairwise,
    aggregate_repetition,
    aggregate_utility_delta,
    build_case_run_summary,
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


def test_pairwise_valid_denominator_excludes_inconsistency_and_errors() -> None:
    outcomes = [
        _outcome("p1", runner="pairwise_replay", pairwise_winner="a", swap_consistent=True),
        _outcome("p2", runner="pairwise_replay", pairwise_winner="b", swap_consistent=True),
        _outcome("p3", runner="pairwise_replay", pairwise_winner="a", swap_consistent=True),
        _outcome("p4", runner="pairwise_replay", pairwise_winner=None, swap_consistent=False,
                 response_status="judge_inconsistency"),
        _outcome("p5", runner="pairwise_replay", pairwise_winner=None,
                 response_status="judge_timeout"),
        _outcome("p6", runner="pairwise_replay", pairwise_winner="tie", swap_consistent=True),
    ]
    result = aggregate_pairwise(outcomes)
    assert result["status"] == "available"
    assert result["valid_pair_count"] == 4
    assert result["wins_a"] == 2
    assert result["wins_b"] == 1
    assert result["ties"] == 1
    assert result["inconsistency_count"] == 1
    assert result["error_count"] == 1
    assert result["win_rate_a"] == 0.5
    assert result["tie_rate"] == 0.25
    assert "model-a_vs_model-b" in result["buckets"]


def test_pairwise_empty_denominator_is_no_data() -> None:
    outcomes = [
        _outcome("p1", runner="pairwise_replay", response_status="judge_inconsistency",
                 swap_consistent=False),
    ]
    result = aggregate_pairwise(outcomes)
    assert result["status"] == "no_data"
    assert result["valid_pair_count"] == 0
    assert result["inconsistency_count"] == 1


def test_ooc_accuracy_needs_gold() -> None:
    with_gold = [
        _outcome("o1", rule_metrics={"ooc": {"response_status": "valid", "predicted": True, "gold": True}}),
        _outcome("o2", rule_metrics={"ooc": {"response_status": "valid", "predicted": False, "gold": True}}),
    ]
    result = aggregate_ooc(with_gold)
    assert result["status"] == "available"
    assert result["accuracy"] == 0.5

    without_gold = [
        _outcome("o3", rule_metrics={"ooc": {"response_status": "valid", "predicted": False}}),
    ]
    assert aggregate_ooc(without_gold)["status"] == "unavailable"
    assert aggregate_ooc([])["status"] == "no_data"


def test_repetition_aggregation() -> None:
    outcomes = [
        _outcome("r1", rule_metrics={"repetition_3gram": {"status": "ok", "rate": 0.2, "total_3grams": 10, "repeated_3grams": 2}}),
        _outcome("r2", rule_metrics={"repetition_3gram": {"status": "ok", "rate": 0.4, "total_3grams": 10, "repeated_3grams": 4}}),
        _outcome("r3", rule_metrics={"repetition_3gram": {"status": "no_data", "rate": None, "total_3grams": 0, "repeated_3grams": 0}}),
    ]
    result = aggregate_repetition(outcomes)
    assert result["status"] == "available"
    assert result["mean_rate"] == 0.3
    assert result["no_data_count"] == 1


def test_memory_aggregation_faithfulness_abstention_injection() -> None:
    outcomes = [
        _outcome("m1", rule_metrics={
            "faithfulness": {"response_status": "valid", "rate": 0.8},
            "memory_usage": {"judge_status": "ok", "retrieved_ids": ["a", "b"], "used_ids": ["a"]},
        }),
        _outcome("m2", rule_metrics={
            "faithfulness": {"response_status": "valid", "rate": 1.0},
            "abstention": {"response_status": "valid", "predicted": True, "gold": True, "correct": True},
            "memory_usage": {"judge_status": "unavailable"},
            "retrieval_dependent": {"status": "hook_required"},
        }),
    ]
    result = aggregate_memory(outcomes)
    assert result["faithfulness"]["mean_rate"] == 0.9
    assert result["abstention_accuracy"]["accuracy"] == 1.0
    assert result["injection_hit_rate"]["mean_rate"] == 0.5
    assert result["recall_precision_update"]["status"] == "hook_required"


def test_utility_delta_pairs_on_off() -> None:
    def pair(case_id: str, pair_id: str, active: bool, persona: int) -> CaseOutcome:
        return _outcome(
            case_id,
            dimension_scores={"persona_fidelity": persona, "coherence_progression": persona},
            rule_metrics={
                "metric_kind": "memory_utility_pair",
                "utility_pair_id": pair_id,
                "memory": {"memory_active": active},
            },
        )

    outcomes = [
        pair("on-1", "up-1", True, 5),
        pair("off-1", "up-1", False, 3),
        pair("on-2", "up-2", True, 4),
        # missing off side -> excluded
    ]
    result = aggregate_utility_delta(outcomes)
    assert result["status"] == "available"
    assert result["valid_pair_count"] == 1
    persona = result["dimensions"]["persona_fidelity"]
    assert persona["mean_delta"] == 2.0
    assert persona["positive_rate"] == 1.0


def test_utility_delta_no_pairs_no_data() -> None:
    assert aggregate_utility_delta([])["status"] == "no_data"


def test_build_case_run_summary_contains_all_sections() -> None:
    summary = build_case_run_summary([])
    assert set(summary) == {
        "dimensions",
        "ooc",
        "pairwise",
        "repetition",
        "memory",
        "memory_utility_delta",
    }
