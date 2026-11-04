"""Loader contract tests: fail-closed, manifest-driven, no_data semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.evaluation.datasets.loaders import DatasetLoader
from src.evaluation.errors import DatasetLoadError
from tests.unit.evaluation.fixture_factory import (
    make_annotation,
    make_memory_case,
    make_pairwise_case,
    make_roleplay_case,
    make_utility_pair,
    write_dataset,
)


def test_loads_valid_fixture(tmp_path: Path) -> None:
    roleplay = [make_roleplay_case("rp-b"), make_roleplay_case("rp-a")]
    memory = [make_memory_case("mem-a")]
    pairwise = [make_pairwise_case(roleplay[1], case_id="pair-a")]
    annotations = [
        make_annotation("rp-a", 1, "Calibration response text."),
        make_annotation("rp-a", 2, "Calibration response text."),
    ]
    write_dataset(
        tmp_path, roleplay=roleplay, memory=memory, pairwise=pairwise, annotations=annotations
    )

    loaded = DatasetLoader(tmp_path).load()

    assert loaded.dataset_id == "parlasoul_gold"
    assert loaded.dataset_version == "v1.0.0"
    assert loaded.text_case_count == 3
    # stable ordering by case_id
    assert [c.case_id for c in loaded.roleplay_cases] == ["rp-a", "rp-b"]
    assert len(loaded.pairwise_cases) == 1
    assert len(loaded.annotations) == 2
    assert "roleplay" in loaded.file_fingerprints
    assert loaded.base_cases["rp-a"].case_id == "rp-a"


def test_missing_manifest_fails(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(DatasetLoadError, match="manifest"):
        DatasetLoader(tmp_path).load()


def test_manifest_fingerprint_tamper_fails(tmp_path: Path) -> None:
    write_dataset(tmp_path, manifest_overrides={"dataset_id": "parlasoul_gold"})
    manifest_path = tmp_path / "manifest.json"
    text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(text.replace("parlasoul_gold", "evil_gold"), encoding="utf-8")

    with pytest.raises(DatasetLoadError, match="identity|fingerprint"):
        DatasetLoader(tmp_path).load()


def test_file_sha_mismatch_fails(tmp_path: Path) -> None:
    write_dataset(tmp_path, corrupt_file_sha="roleplay")
    with pytest.raises(DatasetLoadError, match="sha256 mismatch"):
        DatasetLoader(tmp_path).load()


def test_empty_jsonl_is_legal_no_data_input(tmp_path: Path) -> None:
    write_dataset(tmp_path, roleplay=[], memory=[], pairwise=[], annotations=[], consolidation=[])
    loaded = DatasetLoader(tmp_path).load()
    assert loaded.text_case_count == 0
    assert loaded.pairwise_cases == ()


def test_duplicate_case_id_rejected(tmp_path: Path) -> None:
    write_dataset(
        tmp_path,
        roleplay=[make_roleplay_case("dup-id"), make_roleplay_case("dup-id", user_message="Other msg")],
    )
    with pytest.raises(DatasetLoadError, match="duplicate case_id"):
        DatasetLoader(tmp_path).load()


def test_corrupted_file_bytes_detected_by_sha(tmp_path: Path) -> None:
    write_dataset(tmp_path)
    (tmp_path / "roleplay.jsonl").write_bytes(b"\xff\xfe invalid")
    with pytest.raises(DatasetLoadError, match="sha256 mismatch"):
        DatasetLoader(tmp_path).load()


def test_non_utf8_manifest_rejected(tmp_path: Path) -> None:
    write_dataset(tmp_path)
    (tmp_path / "manifest.json").write_bytes(b"\xff\xfe{")
    with pytest.raises(DatasetLoadError, match="UTF-8"):
        DatasetLoader(tmp_path).load()


def test_pairwise_fingerprint_mismatch_rejected(tmp_path: Path) -> None:
    base = make_roleplay_case("rp-base")
    pair = make_pairwise_case(base, case_id="pair-x")
    pair["base_context_fingerprint"] = "0" * 64
    write_dataset(tmp_path, roleplay=[base], pairwise=[pair])
    with pytest.raises(DatasetLoadError, match="base_context_fingerprint"):
        DatasetLoader(tmp_path).load()


def test_pairwise_unresolved_base_rejected(tmp_path: Path) -> None:
    pair = make_pairwise_case(make_roleplay_case("missing-base"), case_id="pair-y")
    write_dataset(tmp_path, roleplay=[], pairwise=[pair])
    with pytest.raises(DatasetLoadError, match="base_case_id"):
        DatasetLoader(tmp_path).load()


def test_variant_content_hash_mismatch_rejected(tmp_path: Path) -> None:
    base = make_roleplay_case("rp-base2")
    pair = make_pairwise_case(base, case_id="pair-z")
    pair["variant_a"]["content"] = "tampered content"
    write_dataset(tmp_path, roleplay=[base], pairwise=[pair])
    with pytest.raises(DatasetLoadError, match="source_fingerprint"):
        DatasetLoader(tmp_path).load()


def test_utility_pair_context_drift_rejected(tmp_path: Path) -> None:
    on, off = make_utility_pair()
    off["user_message"] = "a different question"
    write_dataset(tmp_path, roleplay=[], memory=[on, off])
    with pytest.raises(DatasetLoadError, match="utility_pair"):
        DatasetLoader(tmp_path).load()


def test_split_leakage_rejected(tmp_path: Path) -> None:
    smoke_case = make_roleplay_case("rp-smoke", split="smoke")
    test_case = make_roleplay_case("rp-test", split="test", user_message="Unique question here.")
    leaked = make_roleplay_case("rp-leak", split="dev")  # identical context to rp-smoke
    leaked["history_turns"] = list(smoke_case["history_turns"])
    leaked["user_message"] = smoke_case["user_message"]
    write_dataset(tmp_path, roleplay=[smoke_case, test_case, leaked])
    with pytest.raises(DatasetLoadError, match="split leakage"):
        DatasetLoader(tmp_path).load()


def test_abstention_case_with_evidence_rejected(tmp_path: Path) -> None:
    case = make_memory_case("mem-abs", task_type="memory_abstention", gold_memory_ids=[])
    case["should_abstain"] = True
    case["supporting_memory_ids"] = ["m-fixture-001"]
    write_dataset(tmp_path, roleplay=[], memory=[case])
    with pytest.raises(DatasetLoadError, match="abstention"):
        DatasetLoader(tmp_path).load()


def test_single_annotator_calibration_rejected(tmp_path: Path) -> None:
    write_dataset(
        tmp_path,
        annotations=[make_annotation("rp-a", 1, "Calibration response text.")],
    )
    with pytest.raises(DatasetLoadError, match="annotators"):
        DatasetLoader(tmp_path).load()


def test_image_url_rejected(tmp_path: Path) -> None:
    case = make_roleplay_case("rp-img", user_message_images=["https://evil.example/x.png"])
    write_dataset(tmp_path, roleplay=[case])
    with pytest.raises(DatasetLoadError, match="schema validation"):
        DatasetLoader(tmp_path).load()
