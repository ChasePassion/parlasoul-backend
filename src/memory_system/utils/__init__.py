"""Utility modules for AI Memory System."""

import numpy as np

from .retry import RetryExecutor


def escape_milvus_string(value: str) -> str:
    """Escape string literals used in Milvus filter expressions."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return escaped


def build_filter(user_id: str | None, character_id: str, extra: str | None = None) -> str:
    parts = [
        f'character_id == "{escape_milvus_string(character_id)}"',
    ]
    if user_id:
        parts.insert(0, f'user_id == "{escape_milvus_string(user_id)}"')
    if extra:
        parts.append(extra)
    return " and ".join(parts)


def normalize(vec) -> np.ndarray:

    """Normalize a vector to unit length."""
    vec = np.asarray(vec)
    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec
    return vec / norm


__all__ = ["RetryExecutor", "escape_milvus_string", "build_filter", "normalize"]

