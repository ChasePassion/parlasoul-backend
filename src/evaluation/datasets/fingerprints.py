"""Fingerprint primitives shared with the dataset repository validator.

``base_context_fingerprint`` MUST stay byte-compatible with
``parlasoul-evaluation-dataset/scripts/validate_eval_dataset.py``; any change
invalidates every pairwise case in every dataset version.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HEX64_CHARS = frozenset("0123456789abcdef")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in HEX64_CHARS for c in value)
    )


def base_context_fingerprint(case: dict[str, Any]) -> str:
    """Canonical context identity for a GoldenCase.

    ``memory_active`` is a runner control, not context, so it is excluded —
    this is also what makes the Memory Utility Delta on/off pair share one
    identity.
    """
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


def content_fingerprint(content: str) -> str:
    """sha256 of normalized content — used for variants/annotation targets."""
    return sha256_text(content)


def dataset_fingerprint_from_manifest(manifest: dict[str, Any]) -> str:
    """sha256 of the manifest minus its own fingerprint field."""
    payload = {k: v for k, v in manifest.items() if k != "fingerprint"}
    return sha256_text(canonical_json(payload))
