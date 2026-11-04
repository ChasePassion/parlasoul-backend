"""Builders for synthetic, privacy-safe evaluation dataset fixtures.

Everything here is deterministic and self-contained; manifests are written
with real SHA-256 values and a valid dataset fingerprint so the loader's
fail-closed checks can be exercised in both directions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

DATASET_ID = "parlasoul_gold"
DATASET_VERSION = "v1.0.0"
SCHEMA_VERSION = "phase18.case.v1"
RUBRIC_VERSION = "parlasoul-roleplay-5d-ooc-v1"
SANITIZER_VERSION = "synthetic-only-v1"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def base_context_fingerprint(case: dict[str, Any]) -> str:
    ctx = {
        "character_id": case.get("character_id"),
        "character_ref": case.get("character_ref"),
        "history_turns": case.get("history_turns", []),
        "user_message": case.get("user_message"),
        "user_message_images": case.get("user_message_images", []),
        "input_transform_policy": case.get("input_transform_policy"),
        "memory_snapshot_present": case.get("memory_snapshot") is not None,
    }
    return sha256_text(canonical_json(ctx))


def make_character_snapshot(name: str = "Alex") -> dict[str, Any]:
    return {
        "name": name,
        "description": "A calm, encouraging English-learning companion.",
        "system_prompt": f"Approved synthetic system prompt for {name}.",
        "facts": [
            {
                "fact_id": f"{name.lower()}-fact-1",
                "text": f"{name} keeps replies short.",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": None,
            }
        ],
        "relationship_state": ["study_partner"],
        "forbidden_behaviors": ["breaking character"],
        "confirmed_facts": ["The user has an interview tomorrow."],
    }


def make_memory_item(
    memory_id: str,
    text: str,
    memory_type: str = "episodic",
    *,
    valid_from: str = "2026-01-10T00:00:00Z",
    valid_to: str | None = None,
    supersedes: list[str] | None = None,
    conflicts_with: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "source_turn_id": "synthetic-turn-1",
        "text": text,
        "memory_type": memory_type,
        "created_at": "2026-01-10T00:00:00Z",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "group_id": None,
        "is_conflicting": False,
        "source_fingerprint": sha256_text(text),
        "supersedes": supersedes or [],
        "conflicts_with": conflicts_with or [],
    }


def make_roleplay_case(
    case_id: str = "rp-fixture-001",
    *,
    split: str = "smoke",
    task_type: str = "roleplay_persona",
    user_message: str = "Can we practise a few interview questions?",
    **overrides: Any,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "case_id": case_id,
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "split": split,
        "language": "en",
        "character_id": None,
        "character_ref": "character.synthetic.alex",
        "character_snapshot": make_character_snapshot(),
        "history_turns": [
            {"role": "user", "content": "I'm nervous about my interview."},
            {"role": "assistant", "content": "That's normal. Let's practise together."},
        ],
        "user_message": user_message,
        "input_transform_policy": "raw",
        "task_type": task_type,
        "expected_behavior": "PRIMARY (persona_fidelity): stay in character and practise.",
        "tags": ["fixture"],
    }
    case.update(overrides)
    return case


def make_memory_case(
    case_id: str = "mem-fixture-001",
    *,
    split: str = "smoke",
    task_type: str = "memory_recall",
    user_message: str = "What do I do for work?",
    gold_memory_ids: list[str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    memory_id = "m-fixture-001"
    case: dict[str, Any] = {
        "case_id": case_id,
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "split": split,
        "language": "en",
        "character_id": None,
        "character_ref": "character.synthetic.alex",
        "character_snapshot": make_character_snapshot(),
        "history_turns": [
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi, good to see you."},
        ],
        "user_message": user_message,
        "input_transform_policy": "raw",
        "task_type": task_type,
        "memory_snapshot": {
            "episodic": [make_memory_item(memory_id, "The user works night shifts as a nurse.")],
            "semantic": [],
        },
        "gold_memory_ids": gold_memory_ids if gold_memory_ids is not None else [memory_id],
        "supporting_memory_ids": [],
        "expected_claims": [],
        "tags": ["fixture"],
    }
    case.update(overrides)
    return case


def make_utility_pair(pair_id: str = "up-1", *, case_id_prefix: str = "mem-util") -> list[dict[str, Any]]:
    snapshot = {
        "episodic": [make_memory_item("m-util-1", "The user prefers jazz.")],
        "semantic": [],
    }
    common = {
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "split": "smoke",
        "language": "en",
        "character_id": None,
        "character_ref": "character.synthetic.alex",
        "character_snapshot": make_character_snapshot(),
        "history_turns": [{"role": "user", "content": "Play some music?"}],
        "user_message": "What music do I like?",
        "input_transform_policy": "raw",
        "task_type": "memory_utility_pair",
        "memory_snapshot": snapshot,
        "tags": [f"utility_pair:{pair_id}"],
    }
    on = dict(common, case_id=f"{case_id_prefix}-on", memory_active=True, expected_answer="Jazz.")
    off = dict(common, case_id=f"{case_id_prefix}-off", memory_active=False, expected_answer=None)
    return [on, off]


def make_pairwise_case(
    base_case: dict[str, Any],
    *,
    case_id: str = "pair-fixture-001",
    split: str = "smoke",
    variant_a_content: str = "Variant A response.",
    variant_b_content: str = "Variant B response.",
    human_winner: str = "a",
    task_type: str = "pairwise_roleplay",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "split": split,
        "task_type": task_type,
        "base_case_id": base_case["case_id"],
        "base_context_fingerprint": base_context_fingerprint(base_case),
        "variant_a": {
            "variant_ref": f"synthetic.variant.a.{case_id}",
            "content": variant_a_content,
            "source_fingerprint": sha256_text(variant_a_content),
            "generation_provider": "fixture",
            "generation_model": "fixture-model-a",
            "generation_model_type": "fixture",
        },
        "variant_b": {
            "variant_ref": f"synthetic.variant.b.{case_id}",
            "content": variant_b_content,
            "source_fingerprint": sha256_text(variant_b_content),
            "generation_provider": "fixture",
            "generation_model": "fixture-model-b",
            "generation_model_type": "fixture",
        },
        "order_swap": True,
        "human_winner": human_winner,
        "winner_provenance": {"source": "synthetic_author", "method": "manual_blind"},
        "tags": ["fixture"],
    }


def make_annotation(
    case_id: str,
    annotator_no: int,
    target_content: str,
    *,
    annotation_id: str | None = None,
    scores: dict[str, int | None] | None = None,
    ooc_label: bool = False,
    adjudication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    annotation_id = annotation_id or f"ann-{case_id}-{annotator_no}"
    return {
        "annotation_id": annotation_id,
        "case_id": case_id,
        "annotator_ref": f"annotator-opaque-0{annotator_no}",
        "rubric_version": RUBRIC_VERSION,
        "target": {
            "response_ref": f"synthetic.response.{case_id}.v1",
            "content": target_content,
            "source_fingerprint": sha256_text(target_content),
        },
        "scores": scores
        or {
            "persona_fidelity": 4,
            "multi_turn_consistency": 4,
            "coherence_progression": 4,
            "empathy_appropriateness": 4,
            "engagement_diversity": 3,
        },
        "ooc_label": ooc_label,
        "memory_labels": None,
        "pairwise_winner": None,
        "claim_refs": [],
        "adjudication": adjudication,
    }


def make_consolidation_batch(batch_id: str = "consolidation-fixture-001") -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "episodic_memories": [
            make_memory_item("e-1", "The user likes jazz."),
            make_memory_item("e-2", "The user likes jazz music."),
        ],
        "semantic_memories": [],
        "expected_actions": [
            {"action": "deduplicate", "source_memory_ids": ["e-1", "e-2"], "output_memory_id": "s-1"}
        ],
        "expected_outputs": [
            {"memory_id": "s-1", "text": "The user likes jazz.", "memory_type": "semantic", "state": "created"}
        ],
        "expected_before": {
            "episodic_count": 2,
            "semantic_count": 0,
            "physical_total_count": 2,
            "logical_fact_count": 2,
        },
        "expected_after": {
            "episodic_count": 2,
            "semantic_count": 1,
            "physical_total_count": 3,
            "logical_fact_count": 1,
        },
        "expected_superseded": [],
        "supporting_source_ids": ["e-1", "e-2"],
    }


REALTIME_SCENARIO: dict[str, Any] = {
    "scenario_id": "realtime-fixture-001",
    "description": "Normal wifi turn fixture.",
    "network_profile": "wifi_normal",
    "network_params": {"rtt_ms": 30, "jitter_ms": 5, "packet_loss_ratio": 0.0, "reconnect": False},
    "fixtures": [],
    "actions": [
        {"type": "connect"},
        {"type": "silence", "duration_ms": 500},
        {"type": "disconnect"},
    ],
    "assertions": [
        {"event": "session_started", "within_ms": 1000, "anchor": "connect"},
    ],
    "metrics": ["ttfa_ms"],
    "expected_result": "passed",
}


def build_manifest(
    files: dict[str, dict[str, Any]],
    *,
    manifest_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "schema_version": SCHEMA_VERSION,
        "language": "en",
        "files": files,
        "splits": {},
        "task_types": [],
        "rubric_version": RUBRIC_VERSION,
        "sanitizer_version": SANITIZER_VERSION,
        "data_classification": "synthetic",
        "source_policy": {"production_text": False, "approved_sidecar": False},
        "contracts": {},
        "created_at": "2026-08-14T00:00:00Z",
        "fingerprint_algorithm": "sha256(canonical-json(manifest-minus-fingerprint,keys-sorted,utf8))",
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    payload = {k: v for k, v in manifest.items() if k != "fingerprint"}
    manifest["fingerprint"] = sha256_text(canonical_json(payload))
    return manifest


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def write_dataset(
    root: Path,
    *,
    roleplay: list[dict[str, Any]] | None = None,
    memory: list[dict[str, Any]] | None = None,
    pairwise: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
    consolidation: list[dict[str, Any]] | None = None,
    realtime_scenarios: list[dict[str, Any]] | None = None,
    manifest_overrides: dict[str, Any] | None = None,
    corrupt_file_sha: str | None = None,
) -> dict[str, Any]:
    """Write a complete dataset directory and return the manifest dict.

    ``corrupt_file_sha`` names a file kind whose manifest sha256 is tampered
    with (to exercise fail-closed integrity checks).
    """
    import yaml

    root.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, list[dict[str, Any]]] = {
        "roleplay": roleplay if roleplay is not None else [make_roleplay_case()],
        "memory": memory if memory is not None else [make_memory_case()],
        "pairwise": pairwise if pairwise is not None else [],
        "annotations": annotations if annotations is not None else [],
        "consolidation": consolidation if consolidation is not None else [],
    }
    file_names = {
        "roleplay": "roleplay.jsonl",
        "memory": "memory.jsonl",
        "pairwise": "pairwise.jsonl",
        "annotations": "annotations.jsonl",
        "consolidation": "consolidation.jsonl",
        "realtime": "realtime-scenarios.yaml",
    }

    files: dict[str, dict[str, Any]] = {}
    for kind, rows in payloads.items():
        path = root / file_names[kind]
        write_jsonl(path, rows)
        files[kind] = {
            "path": file_names[kind],
            "records": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    scenarios = realtime_scenarios if realtime_scenarios is not None else [REALTIME_SCENARIO]
    realtime_path = root / file_names["realtime"]
    realtime_path.write_text(
        yaml.safe_dump_all(scenarios, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    scenario_count = len(scenarios)
    files["realtime"] = {
        "path": file_names["realtime"],
        "records": scenario_count,
        "sha256": hashlib.sha256(realtime_path.read_bytes()).hexdigest(),
    }

    if corrupt_file_sha and corrupt_file_sha in files:
        files[corrupt_file_sha]["sha256"] = "0" * 64

    manifest = build_manifest(files, manifest_overrides=manifest_overrides)
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest
