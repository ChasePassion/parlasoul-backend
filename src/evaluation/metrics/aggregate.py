"""Denominator-explicit aggregation over case outcomes.

Every metric returns numerator/denominator/status; missing gold or traces
yield ``unavailable``/``no_data`` — never a default score.
"""

from __future__ import annotations

from typing import Any

from src.evaluation.constants import DIMENSIONS
from src.evaluation.runners.base import CaseOutcome

Status = str


def _metric(status: Status, **values: Any) -> dict[str, Any]:
    return {"status": status, **values}


def aggregate_dimensions(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    per_dimension: dict[str, dict[str, Any]] = {}
    for dimension in DIMENSIONS:
        scores = [
            outcome.dimension_scores[dimension]
            for outcome in outcomes
            if outcome.response_status == "valid" and dimension in outcome.dimension_scores
        ]
        if not scores:
            per_dimension[dimension] = _metric(
                "no_data", sample_count=0, mean=None, distribution={}
            )
            continue
        distribution: dict[str, int] = {}
        for score in scores:
            distribution[str(score)] = distribution.get(str(score), 0) + 1
        per_dimension[dimension] = _metric(
            "available",
            sample_count=len(scores),
            mean=round(sum(scores) / len(scores), 4),
            distribution=distribution,
        )
    return per_dimension


def aggregate_ooc(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    gold_present = 0
    correct = 0
    predicted = 0
    for outcome in outcomes:
        ooc_metrics = outcome.rule_metrics.get("ooc") or {}
        if ooc_metrics.get("response_status") != "valid":
            continue
        if "predicted" not in ooc_metrics:
            continue
        predicted += 1
        gold = ooc_metrics.get("gold")
        if gold is None:
            continue
        gold_present += 1
        if bool(ooc_metrics["predicted"]) == bool(gold):
            correct += 1
    if predicted == 0:
        return _metric("no_data", gold_count=0, accuracy=None)
    if gold_present == 0:
        return _metric("unavailable", gold_count=0, accuracy=None, predicted_count=predicted)
    return _metric(
        "available",
        gold_count=gold_present,
        correct=correct,
        accuracy=round(correct / gold_present, 4),
        predicted_count=predicted,
    )


def aggregate_pairwise(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    wins_a = wins_b = ties = 0
    valid_pair_count = 0
    inconsistency_count = 0
    error_count = 0
    buckets: dict[str, dict[str, int]] = {}

    for outcome in outcomes:
        if outcome.runner != "pairwise_replay":
            continue
        if outcome.response_status == "judge_inconsistency":
            inconsistency_count += 1
            continue
        if outcome.response_status != "valid" or outcome.pairwise_winner is None:
            error_count += 1
            continue
        valid_pair_count += 1
        if outcome.pairwise_winner == "a":
            wins_a += 1
        elif outcome.pairwise_winner == "b":
            wins_b += 1
        else:
            ties += 1
        bucket_key = (
            f"{outcome.variant_a_generation_model or 'unknown'}"
            f"_vs_{outcome.variant_b_generation_model or 'unknown'}"
        )
        bucket = buckets.setdefault(bucket_key, {"wins_a": 0, "wins_b": 0, "ties": 0})
        if outcome.pairwise_winner == "tie":
            bucket["ties"] += 1
        else:
            bucket[f"wins_{outcome.pairwise_winner}"] += 1

    if valid_pair_count == 0:
        return _metric(
            "no_data",
            wins_a=0,
            wins_b=0,
            ties=0,
            valid_pair_count=0,
            inconsistency_count=inconsistency_count,
            error_count=error_count,
        )
    return _metric(
        "available",
        wins_a=wins_a,
        wins_b=wins_b,
        ties=ties,
        valid_pair_count=valid_pair_count,
        win_rate_a=round(wins_a / valid_pair_count, 4),
        win_rate_b=round(wins_b / valid_pair_count, 4),
        tie_rate=round(ties / valid_pair_count, 4),
        inconsistency_count=inconsistency_count,
        error_count=error_count,
        buckets=buckets,
    )


def aggregate_repetition(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    rates: list[float] = []
    no_data = 0
    for outcome in outcomes:
        rep = outcome.rule_metrics.get("repetition_3gram") or {}
        if rep.get("status") == "no_data":
            no_data += 1
            continue
        if rep.get("status") == "ok" and rep.get("rate") is not None:
            rates.append(float(rep["rate"]))
    if not rates:
        return _metric("no_data", sample_count=0, mean_rate=None, no_data_count=no_data)
    return _metric(
        "available",
        sample_count=len(rates),
        mean_rate=round(sum(rates) / len(rates), 6),
        no_data_count=no_data,
    )


def aggregate_memory(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """Memory-side metrics from rule_metrics traces.

    Faithfulness / abstention / injection come from generation-side judgments
    and usage traces. Recall/Precision/Conflict/Update need a live retrieval
    provenance trace and stay ``hook_required`` until one exists.
    """
    faithfulness_rates: list[float] = []
    abstention_total = 0
    abstention_correct = 0
    injection_rates: list[float] = []
    retrieval_dependent_seen = 0

    for outcome in outcomes:
        faithfulness = outcome.rule_metrics.get("faithfulness") or {}
        if faithfulness.get("response_status") == "valid" and faithfulness.get("rate") is not None:
            faithfulness_rates.append(float(faithfulness["rate"]))

        abstention = outcome.rule_metrics.get("abstention") or {}
        if abstention.get("response_status") == "valid" and abstention.get("correct") is not None:
            abstention_total += 1
            if abstention["correct"]:
                abstention_correct += 1

        usage = outcome.rule_metrics.get("memory_usage") or {}
        if usage.get("judge_status") == "ok":
            retrieved = usage.get("retrieved_ids") or []
            used = usage.get("used_ids") or []
            if retrieved:
                hit = len(set(used) & set(retrieved)) / len(retrieved)
                injection_rates.append(hit)

        if "retrieval_dependent" in outcome.rule_metrics:
            retrieval_dependent_seen += 1

    result: dict[str, Any] = {}
    result["faithfulness"] = (
        _metric(
            "available",
            sample_count=len(faithfulness_rates),
            mean_rate=round(sum(faithfulness_rates) / len(faithfulness_rates), 4),
        )
        if faithfulness_rates
        else _metric("no_data", sample_count=0, mean_rate=None)
    )
    result["abstention_accuracy"] = (
        _metric(
            "available",
            sample_count=abstention_total,
            correct=abstention_correct,
            accuracy=round(abstention_correct / abstention_total, 4),
        )
        if abstention_total
        else _metric("no_data", sample_count=0, accuracy=None)
    )
    result["injection_hit_rate"] = (
        _metric(
            "available",
            sample_count=len(injection_rates),
            mean_rate=round(sum(injection_rates) / len(injection_rates), 4),
        )
        if injection_rates
        else _metric("no_data", sample_count=0, mean_rate=None)
    )
    result["recall_precision_update"] = _metric(
        "hook_required" if retrieval_dependent_seen else "unavailable",
        sample_count=0,
        reason="retrieval_provenance_trace_required",
    )
    return result


def aggregate_utility_delta(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """Memory Utility Delta over paired on/off cases.

    Pairs are matched by ``utility_pair_id``; both sides must be valid. The
    delta is computed per dimension; a missing side excludes the pair.
    """
    pairs: dict[str, dict[str, CaseOutcome]] = {}
    for outcome in outcomes:
        if outcome.rule_metrics.get("metric_kind") != "memory_utility_pair":
            continue
        pair_id = outcome.rule_metrics.get("utility_pair_id")
        if not pair_id:
            continue
        memory_active = (outcome.rule_metrics.get("memory") or {}).get("memory_active")
        side = "on" if memory_active else "off"
        pairs.setdefault(str(pair_id), {})[side] = outcome

    valid_pairs = 0
    deltas: dict[str, list[float]] = {}
    for sides in pairs.values():
        on = sides.get("on")
        off = sides.get("off")
        if on is None or off is None:
            continue
        if on.response_status != "valid" or off.response_status != "valid":
            continue
        valid_pairs += 1
        for dimension in DIMENSIONS:
            score_on = on.dimension_scores.get(dimension)
            score_off = off.dimension_scores.get(dimension)
            if score_on is None or score_off is None:
                continue
            deltas.setdefault(dimension, []).append(score_on - score_off)

    if valid_pairs == 0:
        return _metric("no_data", valid_pair_count=0, dimensions={})

    per_dimension: dict[str, Any] = {}
    for dimension, values in deltas.items():
        positive = sum(1 for value in values if value > 0)
        per_dimension[dimension] = {
            "sample_count": len(values),
            "mean_delta": round(sum(values) / len(values), 4) if values else None,
            "positive_rate": round(positive / len(values), 4) if values else None,
        }
    return _metric("available", valid_pair_count=valid_pairs, dimensions=per_dimension)


def build_case_run_summary(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    return {
        "dimensions": aggregate_dimensions(outcomes),
        "ooc": aggregate_ooc(outcomes),
        "pairwise": aggregate_pairwise(outcomes),
        "repetition": aggregate_repetition(outcomes),
        "memory": aggregate_memory(outcomes),
        "memory_utility_delta": aggregate_utility_delta(outcomes),
    }
