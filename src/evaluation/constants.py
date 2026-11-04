"""Shared evaluation enums and identity constants (phase18 contract)."""

from __future__ import annotations

from typing import Literal

# Five role-play Judge dimensions; an independent style dimension is forbidden.
DIMENSIONS: tuple[str, ...] = (
    "persona_fidelity",
    "multi_turn_consistency",
    "coherence_progression",
    "empathy_appropriateness",
    "engagement_diversity",
)

OOC_DIMENSION = "ooc"

# Rubric locked for phase18; any change requires a new run (never overwrite).
RUBRIC_VERSION = "parlasoul-roleplay-5d-ooc-v1"
PROMPT_TEMPLATE_VERSION = "parlasoul-judge-prompt-v1"
RUNNER_VERSION = "parlasoul-eval-runner-0.1.0"

TASK_TYPES: frozenset[str] = frozenset(
    {
        "roleplay_persona",
        "roleplay_consistency",
        "roleplay_coherence",
        "roleplay_empathy",
        "roleplay_engagement",
        "memory_recall",
        "memory_precision",
        "memory_update",
        "memory_temporal",
        "memory_faithfulness",
        "memory_abstention",
        "memory_ooc",
        "memory_utility_pair",
        "pairwise_roleplay",
        "pairwise_memory",
    }
)

ROLEPLAY_TASK_TYPES: frozenset[str] = frozenset(
    t for t in TASK_TYPES if t.startswith("roleplay_")
)
MEMORY_TASK_TYPES: frozenset[str] = frozenset(
    t for t in TASK_TYPES if t.startswith("memory_") and t != "memory_utility_pair"
)
PAIRWISE_TASK_TYPES: frozenset[str] = frozenset(
    {"pairwise_roleplay", "pairwise_memory"}
)

SPLITS: frozenset[str] = frozenset({"smoke", "dev", "calibration", "test"})
LANGUAGES: frozenset[str] = frozenset({"zh", "en", "mixed"})

CONSOLIDATION_ACTIONS: frozenset[str] = frozenset(
    {"create", "update", "deduplicate", "merge", "delete", "retain", "no_op"}
)
CONSOLIDATION_OUTPUT_STATES: frozenset[str] = frozenset(
    {"created", "updated", "retained", "superseded", "deleted"}
)

RunnerName = Literal[
    "chat_replay", "candidate_replay", "pairwise_replay", "realtime_functional"
]
RUNNERS: tuple[str, ...] = (
    "chat_replay",
    "candidate_replay",
    "pairwise_replay",
    "realtime_functional",
)

RunKind = Literal["case", "aggregate"]
ContentMode = Literal["none", "redacted", "full"]

RunStatus = Literal[
    "running", "completed", "partial", "failed", "cancelled", "no_data"
]
ResultStatus = Literal["completed", "failed", "skipped", "no_data", "cancelled"]
ResponseStatus = Literal[
    "valid",
    "runner_error",
    "judge_timeout",
    "judge_error",
    "judge_parse_failed",
    "judge_inconsistency",
    "skipped",
    "no_data",
    "cancelled",
]

# Metric-level status used by aggregations (never converted into a 0 score).
MetricStatus = Literal["available", "hook_required", "unavailable", "no_data", "ok", "error"]

# CLI exit codes (plan §2.5).
EXIT_OK = 0
EXIT_INVALID_CONFIG_OR_DATASET = 2
EXIT_DEPENDENCY_OR_ROUTE_UNAVAILABLE = 3
EXIT_PARTIAL_OR_FAILED = 4
EXIT_CANCELLED = 130

# Secret/PII denylist for dataset content scanning (mirrors the dataset repo
# validator; additions welcome, removals are contract-breaking).
PII_DENY_PATTERNS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "bearer ",
    "sk-",
    "authorization:",
    "password=",
    "secret=",
)

# Manifest file kinds the loader understands (paths come from the manifest).
DATASET_FILE_KINDS: tuple[str, ...] = (
    "roleplay",
    "memory",
    "pairwise",
    "annotations",
    "consolidation",
    "realtime",
)
JSONL_FILE_KINDS: tuple[str, ...] = (
    "roleplay",
    "memory",
    "pairwise",
    "annotations",
    "consolidation",
)
