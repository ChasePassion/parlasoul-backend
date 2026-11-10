"""Fingerprint byte-compatibility guards (dataset repo validator alignment)."""

from __future__ import annotations

from src.evaluation.datasets.fingerprints import (
    base_context_fingerprint,
    canonical_json,
    is_hex64,
)


def test_canonical_json_sorts_keys_and_keeps_unicode() -> None:
    assert canonical_json({"b": 1, "a": "文"}) == '{"a":"文","b":1}'
    assert canonical_json({"x": [1, {"d": None, "c": 2}]}) == '{"x":[1,{"c":2,"d":null}]}'


def test_base_context_fingerprint_stable_vector() -> None:
    case = {
        "character_id": None,
        "character_ref": "character.synthetic.alex",
        "history_turns": [{"role": "user", "content": "hi"}],
        "user_message": "hello",
        "input_transform_policy": "raw",
        "memory_snapshot": {"episodic": [], "semantic": []},
    }
    first = base_context_fingerprint(case)
    assert is_hex64(first)
    # memory_active and gold fields must not change the identity
    second = base_context_fingerprint({**case, "memory_active": False, "gold_memory_ids": ["m"]})
    assert first == second
    # removing the snapshot changes memory_snapshot_present → new identity
    third = base_context_fingerprint({k: v for k, v in case.items() if k != "memory_snapshot"})
    assert third != first


