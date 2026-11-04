"""Calibration assembly and agreement helper tests."""

from __future__ import annotations

from src.evaluation.datasets.annotations import (
    build_calibration_set,
    compute_dimension_agreement,
)
from src.evaluation.datasets.schema import Annotation


def _annotation(
    annotation_id: str,
    annotator_no: int,
    *,
    persona: int | None = 4,
    ooc: bool = False,
    adjudication: dict | None = None,
) -> Annotation:
    scores = {
        "persona_fidelity": persona,
        "multi_turn_consistency": 4,
        "coherence_progression": 4,
        "empathy_appropriateness": 4,
        "engagement_diversity": None,
    }
    return Annotation.model_validate(
        {
            "annotation_id": annotation_id,
            "case_id": "rp-a",
            "annotator_ref": f"annotator-opaque-0{annotator_no}",
            "rubric_version": "parlasoul-roleplay-5d-ooc-v1",
            "target": {
                "response_ref": "synthetic.response.rp-a.v1",
                "content": "Response under calibration.",
                "source_fingerprint": "a" * 64,
            },
            "scores": scores,
            "ooc_label": ooc,
            "adjudication": adjudication,
        }
    )


def test_dimension_agreement_exact_and_diff() -> None:
    annotations = [
        _annotation("ann-1", 1, persona=5),
        _annotation("ann-2", 2, persona=3),
    ]

    results = {r.dimension: r for r in compute_dimension_agreement(annotations)}

    persona = results["persona_fidelity"]
    assert persona.sample_count == 1
    assert persona.exact_agreement == 0.0
    assert persona.mean_abs_diff == 2.0

    consistency = results["multi_turn_consistency"]
    assert consistency.exact_agreement == 1.0

    # null = N/A dimension yields no pairs
    assert results["engagement_diversity"].sample_count == 0
    assert results["engagement_diversity"].exact_agreement is None


def test_calibration_gold_from_agreement_without_adjudication() -> None:
    calibration = build_calibration_set(
        [_annotation("ann-1", 1), _annotation("ann-2", 2)]
    )

    case = calibration["rp-a"]
    assert case.final_gold_scores is not None
    assert case.final_gold_scores["persona_fidelity"] == 4
    assert case.final_ooc_label is False


def test_calibration_gold_unavailable_on_disagreement_without_adjudication() -> None:
    calibration = build_calibration_set(
        [_annotation("ann-1", 1, persona=5), _annotation("ann-2", 2, persona=2)]
    )

    assert calibration["rp-a"].final_gold_scores is None


def test_adjudication_gold_wins() -> None:
    adjudication = {
        "adjudicator_ref": "annotator-opaque-09",
        "final_gold": {"scores": {"persona_fidelity": 2}, "ooc_label": True},
        "reason": "adjudicated",
    }
    calibration = build_calibration_set(
        [
            _annotation("ann-1", 1, persona=5),
            _annotation("ann-2", 2, persona=2, adjudication=adjudication),
        ]
    )

    case = calibration["rp-a"]
    assert case.final_gold_scores == {"persona_fidelity": 2}
    assert case.final_ooc_label is True
