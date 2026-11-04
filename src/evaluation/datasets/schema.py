"""Pydantic schema for the parlasoul_gold dataset (phase18.case.v1).

Authoritative sources:
- phase18-evaluation-harness.md §2.2 (GoldenCase / PairwiseCase)
- parlasoul-evaluation-dataset schemas/*.schema.json (DSV100 revisions)

The loader contract follows the dataset JSON Schemas; this module mirrors them
as Pydantic models with strict validation. Mutable defaults always use
``Field(default_factory=...)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.evaluation.constants import (
    CONSOLIDATION_ACTIONS,
    CONSOLIDATION_OUTPUT_STATES,
    DIMENSIONS,
    LANGUAGES,
    PAIRWISE_TASK_TYPES,
    SPLITS,
    TASK_TYPES,
)

_CASE_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,120}$"
_MEM_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,160}$"
_HEX64_PATTERN = r"^[0-9a-f]{64}$"
_ANNOTATOR_PATTERN = r"^annotator-opaque-[A-Za-z0-9]+$"

# URL-shaped strings are forbidden in image references (guide §5).
_URL_SHAPE_PATTERN = r"^[a-zA-Z][a-zA-Z0-9+.-]*://"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000)


class MemoryItem(StrictModel):
    memory_id: str = Field(pattern=_MEM_ID_PATTERN)
    source_turn_id: str | None = Field(default=None, max_length=160)
    text: str = Field(min_length=1, max_length=12000)
    memory_type: Literal["episodic", "semantic"]
    created_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    group_id: str | None = Field(default=None, max_length=160)
    is_conflicting: bool = False
    source_fingerprint: str | None = Field(default=None, pattern=_HEX64_PATTERN)
    # Relational supersession/conflict (DSV100-MEM-002): a memory_id keeps one
    # stable identity everywhere; newer memories reference the ids they replace.
    supersedes: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)


class MemorySnapshot(StrictModel):
    episodic: list[MemoryItem] = Field(default_factory=list)
    semantic: list[MemoryItem] = Field(default_factory=list)


class GoldenCase(StrictModel):
    case_id: str = Field(pattern=_CASE_ID_PATTERN)
    character_id: UUID | None = None
    character_ref: str | None = Field(default=None, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=120)
    dataset_version: str = Field(min_length=1, max_length=40)
    split: Literal["smoke", "dev", "calibration", "test"]
    language: Literal["zh", "en", "mixed"]
    character_snapshot: dict[str, Any]
    history_turns: list[HistoryTurn] = Field(default_factory=list)
    user_message: str = Field(min_length=1, max_length=12000)
    user_message_images: list[str] = Field(default_factory=list, max_length=4)
    input_transform_policy: Literal["raw", "approved_transformed"] = "raw"
    input_transform_version: str | None = Field(default=None, max_length=40)
    input_transform_hash: str | None = Field(default=None, pattern=_HEX64_PATTERN)
    task_type: str
    expected_keywords: list[str] = Field(default_factory=list)
    expected_behavior: str | None = Field(default=None, max_length=6000)
    expected_answer: str | None = Field(default=None, max_length=12000)
    expected_claims: list[dict[str, Any]] = Field(default_factory=list)
    gold_memory_ids: list[str] = Field(default_factory=list)
    supporting_memory_ids: list[str] = Field(default_factory=list)
    should_abstain: bool | None = None
    ooc_label: bool | None = None
    dimension_weights: dict[str, float] = Field(default_factory=dict)
    memory_snapshot: MemorySnapshot | None = None
    # Memory Utility Delta runner control (DSV100-MEM-001); only on/off of a
    # utility pair may differ. Not part of the context fingerprint.
    memory_active: bool | None = None
    source_candidate_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("split")
    @classmethod
    def _split_allowed(cls, value: str) -> str:
        if value not in SPLITS:
            raise ValueError(f"invalid split: {value}")
        return value

    @field_validator("language")
    @classmethod
    def _language_allowed(cls, value: str) -> str:
        if value not in LANGUAGES:
            raise ValueError(f"invalid language: {value}")
        return value

    @field_validator("task_type")
    @classmethod
    def _task_type_allowed(cls, value: str) -> str:
        if value not in TASK_TYPES:
            raise ValueError(f"invalid task_type: {value}")
        return value

    @field_validator("user_message_images")
    @classmethod
    def _images_are_not_urls(cls, value: list[str]) -> list[str]:
        import re

        for item in value:
            if re.match(_URL_SHAPE_PATTERN, item):
                raise ValueError("user_message_images must not contain URLs")
        return value

    @field_validator("dimension_weights")
    @classmethod
    def _dimension_weights_valid(cls, value: dict[str, float]) -> dict[str, float]:
        for key, weight in value.items():
            if key not in DIMENSIONS:
                raise ValueError(f"dimension_weights unknown dimension: {key}")
            if weight != weight or weight < 0:  # NaN or negative
                raise ValueError(f"dimension_weights[{key}] must be finite and >= 0")
        return value


class PairwiseVariant(StrictModel):
    variant_ref: str = Field(min_length=1, max_length=200)
    content: str | None = Field(default=None, max_length=12000)
    source_fingerprint: str | None = Field(default=None, pattern=_HEX64_PATTERN)
    generation_provider: str = Field(min_length=1, max_length=40)
    generation_model: str = Field(min_length=1, max_length=120)
    generation_model_type: str = Field(min_length=1, max_length=120)


class WinnerProvenance(StrictModel):
    source: Literal["synthetic_author", "human_annotator", "adjudication"]
    method: str = Field(min_length=1, max_length=400)
    annotator_refs: list[str] = Field(default_factory=list)


class PairwiseCase(StrictModel):
    case_id: str = Field(pattern=_CASE_ID_PATTERN)
    dataset_id: str = Field(min_length=1, max_length=120)
    dataset_version: str = Field(min_length=1, max_length=40)
    split: Literal["smoke", "dev", "calibration", "test"]
    task_type: Literal["pairwise_roleplay", "pairwise_memory"]
    base_case_id: str = Field(pattern=_CASE_ID_PATTERN)
    base_context_fingerprint: str = Field(pattern=_HEX64_PATTERN)
    variant_a: PairwiseVariant
    variant_b: PairwiseVariant
    order_swap: bool = True
    human_winner: Literal["a", "b", "tie", "invalid"]
    winner_provenance: WinnerProvenance
    tags: list[str] = Field(default_factory=list)

    @field_validator("task_type")
    @classmethod
    def _task_type_pairwise(cls, value: str) -> str:
        if value not in PAIRWISE_TASK_TYPES:
            raise ValueError(f"invalid pairwise task_type: {value}")
        return value


class AnnotationTarget(StrictModel):
    response_ref: str = Field(min_length=1, max_length=200)
    content: str | None = Field(default=None, max_length=12000)
    source_fingerprint: str = Field(pattern=_HEX64_PATTERN)


class MemoryLabels(StrictModel):
    relevant_memory_ids: list[str] = Field(default_factory=list)
    distractor_memory_ids: list[str] = Field(default_factory=list)
    expired_memory_ids: list[str] = Field(default_factory=list)
    supporting_memory_ids: list[str] = Field(default_factory=list)
    should_abstain: bool | None = None


class Adjudication(StrictModel):
    adjudicator_ref: str | None = Field(default=None, pattern=_ANNOTATOR_PATTERN)
    final_gold: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=4000)


class Annotation(StrictModel):
    annotation_id: str = Field(pattern=_CASE_ID_PATTERN)
    case_id: str = Field(pattern=_CASE_ID_PATTERN)
    annotator_ref: str = Field(pattern=_ANNOTATOR_PATTERN)
    rubric_version: str = Field(min_length=1, max_length=80)
    target: AnnotationTarget
    # 1..5 per dimension, or null = N/A for this case.
    scores: dict[str, int | None] = Field(default_factory=dict)
    ooc_label: bool | None = None
    memory_labels: MemoryLabels | None = None
    pairwise_winner: Literal["a", "b", "tie", "invalid"] | None = None
    claim_refs: list[str] = Field(default_factory=list)
    adjudication: Adjudication | None = None

    @field_validator("scores")
    @classmethod
    def _scores_valid(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        if not value:
            raise ValueError("scores must not be empty")
        for key, score in value.items():
            if key not in DIMENSIONS:
                raise ValueError(f"scores contains non-allowed dimension: {key}")
            if score is not None and not (1 <= score <= 5):
                raise ValueError(f"scores[{key}] must be int 1..5 or null")
        return value


class ConsolidationMemoryItem(StrictModel):
    """Consolidation input memory.

    parlasoul_gold persists two shapes: the original minimal triple
    (memory_id/text/source_fingerprint) and the full MemoryItem shape
    (identity, validity window, relations). Both are accepted; the full
    fields stay optional so replay can normalize either.
    """

    memory_id: str = Field(pattern=_MEM_ID_PATTERN)
    source_turn_id: str | None = Field(default=None, max_length=160)
    text: str = Field(min_length=1, max_length=12000)
    memory_type: Literal["episodic", "semantic"] | None = None
    created_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    group_id: str | None = Field(default=None, max_length=160)
    is_conflicting: bool = False
    source_fingerprint: str = Field(pattern=_HEX64_PATTERN)
    supersedes: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)


class ConsolidationAction(StrictModel):
    action: str
    source_memory_ids: list[str] = Field(default_factory=list)
    output_memory_id: str | None = None

    @field_validator("action")
    @classmethod
    def _action_allowed(cls, value: str) -> str:
        if value not in CONSOLIDATION_ACTIONS:
            raise ValueError(f"invalid consolidation action: {value}")
        return value


class ConsolidationOutput(StrictModel):
    memory_id: str = Field(pattern=_MEM_ID_PATTERN)
    text: str = Field(min_length=1, max_length=12000)
    memory_type: Literal["episodic", "semantic"]
    state: str

    @field_validator("state")
    @classmethod
    def _state_allowed(cls, value: str) -> str:
        if value not in CONSOLIDATION_OUTPUT_STATES:
            raise ValueError(f"invalid consolidation output state: {value}")
        return value


class ConsolidationSuperseded(StrictModel):
    memory_id: str = Field(pattern=_MEM_ID_PATTERN)
    superseded_by: str = Field(pattern=_MEM_ID_PATTERN)


class ConsolidationCounts(StrictModel):
    episodic_count: int = Field(ge=0)
    semantic_count: int = Field(ge=0)
    physical_total_count: int = Field(ge=0)
    logical_fact_count: int = Field(ge=0)


class ConsolidationBatch(StrictModel):
    batch_id: str = Field(pattern=_CASE_ID_PATTERN)
    dataset_id: str = Field(min_length=1, max_length=120)
    dataset_version: str = Field(min_length=1, max_length=40)
    episodic_memories: list[ConsolidationMemoryItem] = Field(default_factory=list)
    semantic_memories: list[ConsolidationMemoryItem] = Field(default_factory=list)
    expected_actions: list[ConsolidationAction] = Field(default_factory=list)
    expected_outputs: list[ConsolidationOutput] = Field(default_factory=list)
    expected_before: ConsolidationCounts
    expected_after: ConsolidationCounts
    expected_superseded: list[ConsolidationSuperseded] = Field(default_factory=list)
    supporting_source_ids: list[str] = Field(default_factory=list)


class RealtimeFixture(StrictModel):
    key: str = Field(min_length=1, max_length=400)
    seed: int = Field(ge=0)
    sha256: str = Field(pattern=_HEX64_PATTERN)
    generator: str = Field(min_length=1, max_length=400)
    format: str = Field(min_length=1, max_length=80)
    duration_ms: int | None = Field(default=None, ge=0)


class RealtimeAction(StrictModel):
    type: Literal[
        "connect",
        "send_user_audio_fixture",
        "barge_in",
        "silence",
        "disconnect",
        "simulate_network_drop",
        "reconnect",
    ]
    at_ms: int | None = Field(default=None, ge=0)
    fixture_key: str | None = Field(default=None, max_length=400)
    duration_ms: int | None = Field(default=None, ge=0)


class RealtimeAssertion(StrictModel):
    event: str = Field(min_length=1, max_length=200)
    within_ms: int | None = Field(default=None, ge=0)
    eventually: bool | None = None
    anchor: str | None = Field(default=None, max_length=200)


class RealtimeNetworkParams(StrictModel):
    rtt_ms: int | None = Field(default=None, ge=0)
    jitter_ms: int | None = Field(default=None, ge=0)
    packet_loss_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    reconnect: bool | None = None


class RealtimeScenario(StrictModel):
    scenario_id: str = Field(pattern=_CASE_ID_PATTERN)
    description: str | None = Field(default=None, max_length=4000)
    network_profile: str = Field(min_length=1, max_length=80)
    network_params: RealtimeNetworkParams | None = None
    fixtures: list[RealtimeFixture] = Field(default_factory=list)
    actions: list[RealtimeAction] = Field(min_length=1)
    assertions: list[RealtimeAssertion] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    expected_result: Literal["passed", "failed", "no_data"] | None = None


class DatasetManifestFile(StrictModel):
    path: str = Field(min_length=1, max_length=400)
    records: int = Field(ge=0)
    sha256: str = Field(pattern=_HEX64_PATTERN)


class DatasetManifest(StrictModel):
    """Typed view of manifest.json (identity + contracts + file registry)."""

    model_config = ConfigDict(extra="allow")

    dataset_id: str = Field(min_length=1, max_length=120)
    dataset_version: str = Field(min_length=1, max_length=40)
    schema_version: str = Field(min_length=1, max_length=80)
    language: Literal["zh", "en", "mixed"] = "en"
    files: dict[str, DatasetManifestFile] = Field(default_factory=dict)
    rubric_version: str = Field(min_length=1, max_length=80)
    sanitizer_version: str = Field(min_length=1, max_length=80)
    data_classification: str = Field(min_length=1, max_length=40)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    contracts: dict[str, Any] = Field(default_factory=dict)
    fingerprint_algorithm: str | None = None
    fingerprint: str = Field(pattern=_HEX64_PATTERN)
