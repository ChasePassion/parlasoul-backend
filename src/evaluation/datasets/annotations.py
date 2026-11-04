"""Calibration annotation assembly and Judge–Human agreement helpers.

Human gold never flows into Judge prompts; it is only used to score the Judge
after the fact (agreement / version drift).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.evaluation.constants import DIMENSIONS
from src.evaluation.datasets.schema import Annotation


@dataclass(frozen=True)
class DimensionAgreement:
    dimension: str
    sample_count: int
    # Fraction of annotator pairs that scored exactly the same.
    exact_agreement: float | None
    # Mean absolute score difference across pairs (lower = closer).
    mean_abs_diff: float | None


@dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    response_ref: str
    annotations: tuple[Annotation, ...]
    # Final gold scores per dimension; None when adjudication is missing and
    # annotators disagree (that case cannot calibrate the Judge).
    final_gold_scores: Mapping[str, int] | None
    final_ooc_label: bool | None


def group_annotations_by_case(
    annotations: tuple[Annotation, ...] | list[Annotation],
) -> dict[str, list[Annotation]]:
    by_case: dict[str, list[Annotation]] = {}
    for annotation in annotations:
        by_case.setdefault(annotation.case_id, []).append(annotation)
    return by_case


def _annotator_pair_scores(
    annotations: list[Annotation], dimension: str
) -> list[tuple[int, int]]:
    valued = [
        annotation.scores.get(dimension)
        for annotation in annotations
        if annotation.scores.get(dimension) is not None
    ]
    pairs: list[tuple[int, int]] = []
    for i in range(len(valued)):
        for j in range(i + 1, len(valued)):
            pairs.append((valued[i], valued[j]))  # type: ignore[list-item]
    return pairs


def compute_dimension_agreement(annotations: list[Annotation]) -> list[DimensionAgreement]:
    results: list[DimensionAgreement] = []
    for dimension in DIMENSIONS:
        pairs = _annotator_pair_scores(annotations, dimension)
        if not pairs:
            results.append(
                DimensionAgreement(
                    dimension=dimension,
                    sample_count=0,
                    exact_agreement=None,
                    mean_abs_diff=None,
                )
            )
            continue
        exact = sum(1 for a, b in pairs if a == b) / len(pairs)
        mean_abs = sum(abs(a - b) for a, b in pairs) / len(pairs)
        results.append(
            DimensionAgreement(
                dimension=dimension,
                sample_count=len(pairs),
                exact_agreement=exact,
                mean_abs_diff=mean_abs,
            )
        )
    return results


def build_calibration_case(case_id: str, annotations: list[Annotation]) -> CalibrationCase:
    response_refs = {annotation.target.response_ref for annotation in annotations}
    response_ref = next(iter(response_refs)) if len(response_refs) == 1 else ""

    adjudication = next(
        (annotation.adjudication for annotation in annotations if annotation.adjudication),
        None,
    )

    final_scores: dict[str, int] | None = None
    adjudicated_scores = (adjudication.final_gold or {}).get("scores") if adjudication else None
    if isinstance(adjudicated_scores, dict) and adjudicated_scores:
        cleaned: dict[str, int] = {}
        for key, value in adjudicated_scores.items():
            if key in DIMENSIONS and isinstance(value, int) and 1 <= value <= 5:
                cleaned[key] = value
        if cleaned:
            final_scores = cleaned
    else:
        # Without adjudication, gold exists only when every annotator agrees.
        agreed: dict[str, int] = {}
        usable = True
        for dimension in DIMENSIONS:
            values = {
                annotation.scores.get(dimension)
                for annotation in annotations
                if annotation.scores.get(dimension) is not None
            }
            if len(values) == 1 and None not in values:
                agreed[dimension] = next(iter(values))
            elif len(values) > 1:
                usable = False
                break
        if usable and agreed:
            final_scores = agreed

    ooc_labels = {annotation.ooc_label for annotation in annotations if annotation.ooc_label is not None}
    if adjudication and isinstance(adjudication.final_gold.get("ooc_label"), bool):
        final_ooc: bool | None = adjudication.final_gold["ooc_label"]
    elif len(ooc_labels) == 1:
        final_ooc = next(iter(ooc_labels))
    else:
        final_ooc = None

    return CalibrationCase(
        case_id=case_id,
        response_ref=response_ref,
        annotations=tuple(annotations),
        final_gold_scores=final_scores,
        final_ooc_label=final_ooc,
    )


def build_calibration_set(
    annotations: tuple[Annotation, ...] | list[Annotation],
) -> dict[str, CalibrationCase]:
    by_case = group_annotations_by_case(annotations)
    return {
        case_id: build_calibration_case(case_id, case_annotations)
        for case_id, case_annotations in sorted(by_case.items())
    }
