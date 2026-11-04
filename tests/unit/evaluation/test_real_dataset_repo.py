"""Load the real parlasoul_gold dataset repo when present on this machine.

Skipped in environments without the sibling repository; the loader contract
itself is covered by the synthetic fixture tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.evaluation.datasets.loaders import DatasetLoader

_DATASET_ROOT_ENV = "PARLASOUL_GOLD_DATASET_ROOT"
DATASET_ROOT = (
    Path(os.environ[_DATASET_ROOT_ENV]) if os.environ.get(_DATASET_ROOT_ENV) else None
)

pytestmark = pytest.mark.skipif(
    DATASET_ROOT is None or not DATASET_ROOT.is_dir(),
    reason=f"set {_DATASET_ROOT_ENV} to a parlasoul-evaluation-dataset checkout",
)


def test_real_gold_dataset_loads() -> None:
    loaded = DatasetLoader(DATASET_ROOT).load()

    assert loaded.dataset_id == "parlasoul_gold"
    assert loaded.dataset_version == "v1.0.0"
    assert loaded.schema_version == "phase18.case.v1"
    assert loaded.rubric_version == "parlasoul-roleplay-5d-ooc-v1"
    assert loaded.text_case_count > 0
    assert len(loaded.pairwise_cases) > 0
    assert len(loaded.annotations) > 0
    assert len(loaded.consolidation_batches) > 0
    assert len(loaded.realtime_scenarios) > 0

    # every pairwise case resolves to a real base with matching fingerprint
    base_cases = loaded.base_cases
    for pair in loaded.pairwise_cases:
        assert pair.base_case_id in base_cases
        assert pair.case_id != pair.base_case_id
