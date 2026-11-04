#!/usr/bin/env python3
"""Validate a ParlaSoul gold dataset directory via the phase18 loader.

Runs schema validation, relational gates (duplicate IDs, split leakage,
memory/evidence references, abstention/OOC gold, pairwise fingerprints,
dual-annotation integrity), PII scanning and the manifest fingerprint check.
A failing dataset must never start an evaluation run.

Usage:
    python scripts/validate_eval_dataset.py <dataset-root> [--split SPLIT]

Exit codes: 0 valid, 2 invalid dataset / config error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.evaluation.datasets.loaders import DatasetLoader  # noqa: E402
from src.evaluation.errors import EvaluationError  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate a ParlaSoul gold dataset.")
    parser.add_argument("root", help="dataset directory containing manifest.json")
    parser.add_argument("--split", default=None, help="only summarize this split")
    args = parser.parse_args(argv)

    try:
        loaded = DatasetLoader(Path(args.root)).load()
    except EvaluationError as exc:
        print(f"FAIL [{exc.code}]: {exc.safe_message}", file=sys.stderr)
        return 2

    print("=== ParlaSoul gold dataset validation (phase18 loader) ===")
    print(f"dataset_id={loaded.dataset_id} version={loaded.dataset_version}")
    print(f"schema_version={loaded.schema_version} rubric={loaded.rubric_version}")
    print(f"fingerprint={loaded.dataset_fingerprint}")
    print(
        f"roleplay={len(loaded.roleplay_cases)} memory={len(loaded.memory_cases)} "
        f"pairwise={len(loaded.pairwise_cases)} annotations={len(loaded.annotations)} "
        f"consolidation={len(loaded.consolidation_batches)} "
        f"realtime={len(loaded.realtime_scenarios)}"
    )
    if args.split:
        text_cases = loaded.cases_for_split(args.split)
        pairwise = loaded.pairwise_for_split(args.split)
        print(f"split={args.split}: text_cases={len(text_cases)} pairwise={len(pairwise)}")
    for warning in loaded.warnings:
        print(f"WARN: {warning}")
    print("OK: dataset is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
