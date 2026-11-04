"""Relational gates over raw dataset rows (fail-closed).

Mirrors the quality gates of DATASET_CONSTRUCTION_GUIDE.md §10 and the DSV100
review rules enforced by the dataset repository validator. All functions take
already JSON-parsed rows and return lists of safe error strings (file/line/ID
references only — never raw case text).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.evaluation.constants import (
    DIMENSIONS,
    MEMORY_TASK_TYPES,
    PII_DENY_PATTERNS,
)
from src.evaluation.datasets.fingerprints import (
    base_context_fingerprint,
    canonical_json,
    content_fingerprint,
    is_hex64,
)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_CHARACTER_SNAPSHOT_REQUIRED_KEYS = (
    "name",
    "description",
    "system_prompt",
    "facts",
    "relationship_state",
)


def _case_label(src: str, case: Mapping[str, Any]) -> str:
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        case_id = "<no-case_id>"
    return f"{src}:{case_id}"


def _memory_ids(case: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    snapshot = case.get("memory_snapshot") or {}
    if not isinstance(snapshot, dict):
        return ids
    for bucket in ("episodic", "semantic"):
        for item in snapshot.get(bucket, []) or []:
            if isinstance(item, dict) and item.get("memory_id"):
                ids.add(str(item["memory_id"]))
    return ids


def check_case_identity(
    case: Mapping[str, Any], src: str, *, expected_dataset_id: str, expected_dataset_version: str
) -> list[str]:
    errors: list[str] = []
    p = _case_label(src, case)
    if case.get("dataset_id") != expected_dataset_id:
        errors.append(f"{p}: dataset_id does not match manifest identity")
    if case.get("dataset_version") != expected_dataset_version:
        errors.append(f"{p}: dataset_version does not match manifest identity")
    if case.get("character_id") is None and not case.get("character_ref"):
        errors.append(f"{p}: requires character_id or character_ref")
    character_id = case.get("character_id")
    if character_id is not None and not _UUID_RE.match(str(character_id)):
        errors.append(f"{p}: character_id is not a UUID")
    snapshot = case.get("character_snapshot")
    if not isinstance(snapshot, dict):
        errors.append(f"{p}: character_snapshot must be an object")
    else:
        for key in _CHARACTER_SNAPSHOT_REQUIRED_KEYS:
            if key not in snapshot:
                errors.append(f"{p}: character_snapshot missing required key '{key}'")
    return errors


def check_input_transform_policy(case: Mapping[str, Any], src: str) -> list[str]:
    p = _case_label(src, case)
    policy = case.get("input_transform_policy")
    version = case.get("input_transform_version")
    transform_hash = case.get("input_transform_hash")
    if policy == "raw" and (version is not None or transform_hash is not None):
        return [f"{p}: raw transform policy must not carry version/hash"]
    if policy == "approved_transformed":
        if not version or not is_hex64(transform_hash):
            return [
                f"{p}: approved_transformed policy requires registered version and 64-hex hash"
            ]
    return []


def check_memory_snapshot(case: Mapping[str, Any], src: str) -> list[str]:
    errors: list[str] = []
    p = _case_label(src, case)
    snapshot = case.get("memory_snapshot")
    if not isinstance(snapshot, dict):
        return errors

    ids: set[str] = set()
    for bucket, want in (("episodic", "episodic"), ("semantic", "semantic")):
        for item in snapshot.get(bucket, []) or []:
            if not isinstance(item, dict):
                errors.append(f"{p}: memory_snapshot.{bucket} item must be an object")
                continue
            memory_id = item.get("memory_id")
            if not isinstance(memory_id, str):
                errors.append(f"{p}: memory item without memory_id in {bucket}")
                continue
            if memory_id in ids:
                errors.append(f"{p}: duplicate memory_id within case")
            ids.add(memory_id)
            if item.get("memory_type") != want:
                errors.append(f"{p}: memory in {bucket} bucket must be memory_type={want}")
            fingerprint = item.get("source_fingerprint")
            if fingerprint is None or not is_hex64(fingerprint):
                errors.append(f"{p}: memory source_fingerprint missing or malformed")

    for field in ("gold_memory_ids", "supporting_memory_ids"):
        for memory_id in case.get(field, []) or []:
            if memory_id not in ids:
                errors.append(f"{p}: {field} references missing memory")

    for claim in case.get("expected_claims", []) or []:
        if not isinstance(claim, dict):
            errors.append(f"{p}: expected_claims item must be an object")
            continue
        for memory_id in claim.get("supporting_memory_ids", []) or []:
            if memory_id not in ids:
                errors.append(f"{p}: expected_claims supporting reference missing")

    if case.get("should_abstain") is True and (
        case.get("supporting_memory_ids")
        or case.get("gold_memory_ids")
        or case.get("expected_claims")
    ):
        errors.append(f"{p}: abstention case must not declare supporting evidence or gold")

    return errors


def check_task_requirements(case: Mapping[str, Any], src: str) -> list[str]:
    errors: list[str] = []
    p = _case_label(src, case)
    task_type = case.get("task_type")
    snapshot = case.get("memory_snapshot")

    if task_type in MEMORY_TASK_TYPES and not isinstance(snapshot, dict):
        errors.append(f"{p}: {task_type} requires memory_snapshot")
    if task_type in ("memory_update", "memory_temporal"):
        valid_froms = set()
        if isinstance(snapshot, dict):
            for bucket in ("episodic", "semantic"):
                for item in snapshot.get(bucket, []) or []:
                    if isinstance(item, dict) and item.get("valid_from"):
                        valid_froms.add(item.get("valid_from"))
        if len(valid_froms) < 2:
            errors.append(f"{p}: {task_type} needs >= 2 distinct valid_from states")
    if task_type == "memory_abstention" and case.get("should_abstain") is not True:
        errors.append(f"{p}: memory_abstention requires should_abstain=true")
    if task_type == "memory_ooc" and case.get("ooc_label") is None:
        errors.append(f"{p}: memory_ooc requires ooc_label")
    if task_type in ("memory_recall", "memory_precision") and not case.get("gold_memory_ids"):
        errors.append(f"{p}: {task_type} requires gold_memory_ids")
    if task_type == "memory_faithfulness" and not case.get("expected_claims"):
        errors.append(f"{p}: memory_faithfulness requires expected_claims")
    return errors


def check_pairwise_row(
    row: Mapping[str, Any],
    base_fingerprints: Mapping[str, str],
    *,
    expected_dataset_id: str,
    expected_dataset_version: str,
) -> list[str]:
    errors: list[str] = []
    case_id = row.get("case_id") if isinstance(row.get("case_id"), str) else "<no-id>"
    p = f"pairwise:{case_id}"
    if row.get("dataset_id") != expected_dataset_id or row.get(
        "dataset_version"
    ) != expected_dataset_version:
        errors.append(f"{p}: dataset identity must match manifest")
    base_case_id = row.get("base_case_id")
    if base_case_id == row.get("case_id"):
        errors.append(f"{p}: case_id must differ from base_case_id")
    if not isinstance(base_case_id, str) or base_case_id not in base_fingerprints:
        errors.append(f"{p}: base_case_id does not resolve to a roleplay/memory case")
    else:
        declared = row.get("base_context_fingerprint")
        if not is_hex64(declared):
            errors.append(f"{p}: base_context_fingerprint malformed")
        elif declared != base_fingerprints[base_case_id]:
            errors.append(f"{p}: base_context_fingerprint mismatch with base case")
    if row.get("human_winner") not in ("a", "b", "tie", "invalid"):
        errors.append(f"{p}: human_winner must be a/b/tie/invalid")
    provenance = row.get("winner_provenance")
    if not isinstance(provenance, dict) or not provenance.get("source"):
        errors.append(f"{p}: winner_provenance required")
    for side in ("variant_a", "variant_b"):
        variant = row.get(side) or {}
        if not isinstance(variant, dict):
            errors.append(f"{p}: {side} must be an object")
            continue
        if not variant.get("variant_ref"):
            errors.append(f"{p}: {side}.variant_ref required")
        for key in ("generation_provider", "generation_model", "generation_model_type"):
            if not variant.get(key):
                errors.append(f"{p}: {side}.{key} required")
        content = variant.get("content")
        fingerprint = variant.get("source_fingerprint")
        if content is None:
            errors.append(f"{p}: {side}.content required (approved sidecars not supported here)")
        elif not is_hex64(fingerprint):
            errors.append(f"{p}: {side}.source_fingerprint malformed")
        elif content_fingerprint(content) != fingerprint:
            errors.append(f"{p}: {side}.source_fingerprint != sha256(content)")
    return errors


def check_annotation_rows(rows: list[dict[str, Any]], *, expected_rubric_version: str) -> list[str]:
    errors: list[str] = []
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        annotation_id = row.get("annotation_id")
        annotation_id = annotation_id if isinstance(annotation_id, str) else "<no-id>"
        p = f"annotations:{annotation_id}"
        if not re.match(r"^annotator-opaque-[A-Za-z0-9]+$", str(row.get("annotator_ref", ""))):
            errors.append(f"{p}: annotator_ref must be an opaque ID")
        if row.get("rubric_version") != expected_rubric_version:
            errors.append(f"{p}: rubric_version mismatch with manifest")
        target = row.get("target") or {}
        content = target.get("content") if isinstance(target, dict) else None
        fingerprint = target.get("source_fingerprint") if isinstance(target, dict) else None
        if content is None:
            errors.append(f"{p}: target.content required")
        elif not is_hex64(fingerprint):
            errors.append(f"{p}: target.source_fingerprint malformed")
        elif content_fingerprint(str(content)) != fingerprint:
            errors.append(f"{p}: target.source_fingerprint != sha256(content)")
        scores = row.get("scores") or {}
        if not isinstance(scores, dict) or not scores:
            errors.append(f"{p}: scores required")
        elif any(key not in DIMENSIONS for key in scores):
            errors.append(f"{p}: scores contain non-allowed dimension")
        if "ooc_label" not in row:
            errors.append(f"{p}: ooc_label required")
        if isinstance(target, dict) and any(
            key in target
            for key in ("provider", "model", "generation_provider", "generation_model")
        ):
            errors.append(f"{p}: calibration target must not expose provider/model")
        case_id = row.get("case_id")
        if isinstance(case_id, str):
            by_case.setdefault(case_id, []).append(row)

    for case_id, case_annotations in by_case.items():
        p = f"annotations:case:{case_id}"
        if len({a.get("annotator_ref") for a in case_annotations}) < 2:
            errors.append(f"{p}: calibration case needs >= 2 distinct annotators")
        if len({(a.get("target") or {}).get("response_ref") for a in case_annotations}) > 1:
            errors.append(f"{p}: annotators must share one response_ref")
        dim_values: dict[str, list[int]] = {}
        for annotation in case_annotations:
            for key, value in (annotation.get("scores") or {}).items():
                if isinstance(value, int):
                    dim_values.setdefault(key, []).append(value)
        disagreement = any(
            max(values) - min(values) > 1 for values in dim_values.values() if values
        )
        disagreement |= len({a.get("ooc_label") for a in case_annotations}) > 1
        supporting_sets = {
            tuple(sorted((a.get("memory_labels") or {}).get("supporting_memory_ids", []) or []))
            for a in case_annotations
            if a.get("memory_labels")
        }
        disagreement |= len(supporting_sets) > 1
        winners = {a.get("pairwise_winner") for a in case_annotations if a.get("pairwise_winner")}
        disagreement |= len(winners) > 1
        if disagreement and not any(a.get("adjudication") for a in case_annotations):
            errors.append(f"{p}: disagreement requires an adjudication record")
    return errors


def check_consolidation_row(
    row: Mapping[str, Any], *, expected_dataset_id: str, expected_dataset_version: str
) -> list[str]:
    errors: list[str] = []
    batch_id = row.get("batch_id") if isinstance(row.get("batch_id"), str) else "<no-id>"
    p = f"consolidation:{batch_id}"
    if row.get("dataset_id") != expected_dataset_id or row.get(
        "dataset_version"
    ) != expected_dataset_version:
        errors.append(f"{p}: dataset identity must match manifest")

    episodic = row.get("episodic_memories", []) or []
    semantic = row.get("semantic_memories", []) or []
    input_ids = {
        item.get("memory_id")
        for item in list(episodic) + list(semantic)
        if isinstance(item, dict)
    }
    for action in row.get("expected_actions", []) or []:
        if not isinstance(action, dict):
            errors.append(f"{p}: expected_actions item must be an object")
            continue
        for memory_id in action.get("source_memory_ids", []) or []:
            if memory_id not in input_ids:
                errors.append(f"{p}: action references missing input memory")

    output_ids: set[str] = set()
    for output in row.get("expected_outputs", []) or []:
        if not isinstance(output, dict):
            errors.append(f"{p}: expected_outputs item must be an object")
            continue
        output_id = output.get("memory_id")
        if output_id in output_ids:
            errors.append(f"{p}: duplicate expected_outputs memory_id")
        output_ids.add(str(output_id))
    for superseded in row.get("expected_superseded", []) or []:
        if not isinstance(superseded, dict):
            errors.append(f"{p}: expected_superseded item must be an object")
            continue
        if superseded.get("memory_id") not in (input_ids | output_ids):
            errors.append(f"{p}: expected_superseded memory not found")
        if superseded.get("superseded_by") not in output_ids:
            errors.append(f"{p}: expected_superseded.superseded_by not in expected_outputs")
    episodic_ids = {item.get("memory_id") for item in episodic if isinstance(item, dict)}
    for source_id in row.get("supporting_source_ids", []) or []:
        if source_id not in episodic_ids:
            errors.append(f"{p}: supporting_source_ids must reference episodic inputs")
    return errors


def check_memory_identity(cases: Iterable[tuple[str, Mapping[str, Any]]]) -> list[str]:
    """DSV100-MEM-002: one memory_id keeps a single stable identity."""
    errors: list[str] = []
    by_id: dict[str, list[tuple[str, tuple[Any, ...]]]] = {}
    for src, case in cases:
        snapshot = case.get("memory_snapshot") or {}
        if not isinstance(snapshot, dict):
            continue
        for bucket in ("episodic", "semantic"):
            for item in snapshot.get(bucket, []) or []:
                if not isinstance(item, dict):
                    continue
                memory_id = item.get("memory_id")
                if not memory_id:
                    continue
                identity = (
                    item.get("text"),
                    item.get("memory_type"),
                    item.get("created_at"),
                    item.get("valid_from"),
                    item.get("valid_to"),
                    item.get("group_id"),
                    item.get("is_conflicting"),
                    tuple(item.get("supersedes") or []),
                    tuple(item.get("conflicts_with") or []),
                )
                by_id.setdefault(str(memory_id), []).append((f"{src}:{case.get('case_id')}", identity))
    for memory_id, occurrences in by_id.items():
        identities = {identity for _, identity in occurrences}
        if len(identities) > 1:
            errors.append("memory identity drift for memory_id across cases (DSV100-MEM-002)")
    return errors


def check_utility_pairs(memory_rows: list[dict[str, Any]]) -> list[str]:
    """DSV100-MEM-001: on/off pair shares context; only memory_active differs."""
    errors: list[str] = []
    pairs: dict[str, list[dict[str, Any]]] = {}
    for row in memory_rows:
        if row.get("task_type") != "memory_utility_pair":
            continue
        pair_id = next(
            (
                tag.split(":", 1)[1]
                for tag in row.get("tags", []) or []
                if isinstance(tag, str) and tag.startswith("utility_pair:")
            ),
            None,
        )
        if not pair_id:
            errors.append(f"memory:{row.get('case_id')}: utility_pair case lacks utility_pair tag")
            continue
        pairs.setdefault(pair_id, []).append(row)

    for pair_id, members in pairs.items():
        p = f"utility_pair:{pair_id}"
        if len(members) != 2:
            errors.append(f"{p}: expected exactly 2 members (on/off)")
            continue
        on = [m for m in members if m.get("memory_active") is True]
        off = [m for m in members if m.get("memory_active") is False]
        if len(on) != 1 or len(off) != 1:
            errors.append(f"{p}: need one memory_active=true and one memory_active=false")
            continue
        for field in (
            "character_ref",
            "history_turns",
            "user_message",
            "input_transform_policy",
            "memory_snapshot",
        ):
            if on[0].get(field) != off[0].get(field):
                errors.append(f"{p}: on/off differ in shared context field '{field}'")
    return errors


def check_split_leakage(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    signature_to_splits: dict[str, set[str]] = {}
    for row in rows:
        signature = canonical_json(
            [row.get("character_ref"), row.get("history_turns"), row.get("user_message")]
        )
        split = row.get("split")
        if isinstance(split, str):
            signature_to_splits.setdefault(signature, set()).add(split)
    for splits in signature_to_splits.values():
        if len(splits) > 1:
            return ["split leakage: identical conversation signature appears in multiple splits"]
    return []


def scan_pii(obj: Any, path: str, errors: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            scan_pii(value, f"{path}.{key}", errors)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            scan_pii(value, f"{path}[{index}]", errors)
    elif isinstance(obj, str):
        lowered = obj.lower()
        for pattern in PII_DENY_PATTERNS:
            if pattern in lowered:
                errors.append(f"privacy: possible secret/PII marker at {path}")
                return


def check_json_serializable_fingerprints(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Ensure fingerprint inputs serialize (guards against exotic types)."""
    errors: list[str] = []
    for row in rows:
        try:
            base_context_fingerprint(dict(row))
        except (TypeError, ValueError):
            errors.append(f"case:{row.get('case_id')}: context not fingerprint-serializable")
    return errors


def validate_relations(
    *,
    roleplay_rows: list[dict[str, Any]],
    memory_rows: list[dict[str, Any]],
    pairwise_rows: list[dict[str, Any]],
    annotation_rows: list[dict[str, Any]],
    consolidation_rows: list[dict[str, Any]],
    base_fingerprints: Mapping[str, str],
    expected_dataset_id: str,
    expected_dataset_version: str,
    expected_rubric_version: str,
) -> list[str]:
    errors: list[str] = []

    for src, rows in (("roleplay", roleplay_rows), ("memory", memory_rows)):
        for row in rows:
            errors.extend(
                check_case_identity(
                    row,
                    src,
                    expected_dataset_id=expected_dataset_id,
                    expected_dataset_version=expected_dataset_version,
                )
            )
            errors.extend(check_input_transform_policy(row, src))
            errors.extend(check_memory_snapshot(row, src))
            errors.extend(check_task_requirements(row, src))

    for row in pairwise_rows:
        errors.extend(
            check_pairwise_row(
                row,
                base_fingerprints,
                expected_dataset_id=expected_dataset_id,
                expected_dataset_version=expected_dataset_version,
            )
        )
    errors.extend(
        check_annotation_rows(annotation_rows, expected_rubric_version=expected_rubric_version)
    )
    for row in consolidation_rows:
        errors.extend(
            check_consolidation_row(
                row,
                expected_dataset_id=expected_dataset_id,
                expected_dataset_version=expected_dataset_version,
            )
        )

    errors.extend(
        check_memory_identity(
            [("roleplay", r) for r in roleplay_rows] + [("memory", r) for r in memory_rows]
        )
    )
    errors.extend(check_utility_pairs(memory_rows))
    errors.extend(check_split_leakage(list(roleplay_rows) + list(memory_rows)))

    # Global case_id uniqueness across roleplay/memory/pairwise.
    seen: set[str] = set()
    for row in list(roleplay_rows) + list(memory_rows) + list(pairwise_rows):
        case_id = row.get("case_id")
        if not isinstance(case_id, str):
            continue
        if case_id in seen:
            errors.append(f"duplicate case_id across dataset: {case_id}")
        seen.add(case_id)
    for kind, rows, id_field in (
        ("annotations", annotation_rows, "annotation_id"),
        ("consolidation", consolidation_rows, "batch_id"),
    ):
        ids: set[str] = set()
        for row in rows:
            identity = row.get(id_field)
            if isinstance(identity, str):
                if identity in ids:
                    errors.append(f"duplicate {kind} id: {identity}")
                ids.add(identity)

    for row in list(roleplay_rows) + list(memory_rows) + list(pairwise_rows):
        scan_pii(row, f"case:{row.get('case_id')}", errors)
    for row in annotation_rows:
        scan_pii(row, f"annotation:{row.get('annotation_id')}", errors)
    for row in consolidation_rows:
        scan_pii(row, f"consolidation:{row.get('batch_id')}", errors)

    # dedupe while preserving order for stable reporting
    unique: list[str] = []
    seen_messages: set[str] = set()
    for error in errors:
        if error not in seen_messages:
            seen_messages.add(error)
            unique.append(error)
    return unique
