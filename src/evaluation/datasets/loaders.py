"""Manifest-driven golden dataset loader (fail-closed).

Contract highlights:
- The manifest is the file registry; the loader never hardcodes file names.
- File byte SHA-256 must match the manifest; the dataset fingerprint is
  recomputed and must equal the declared value.
- UTF-8 JSONL, per-line errors with safe codes, duplicate-ID rejection,
  stable ordering by case_id, byte/record caps.
- Empty files are legal no_data inputs; a missing manifest is fatal.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from src.evaluation.constants import DATASET_FILE_KINDS, JSONL_FILE_KINDS
from src.evaluation.datasets.fingerprints import (
    base_context_fingerprint,
    dataset_fingerprint_from_manifest,
    sha256_file,
)
from src.evaluation.datasets.schema import (
    Annotation,
    ConsolidationBatch,
    DatasetManifest,
    GoldenCase,
    PairwiseCase,
    RealtimeScenario,
)
from src.evaluation.datasets.validator import validate_relations
from src.evaluation.errors import DatasetLoadError

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_RECORDS_PER_FILE = 200_000


@dataclass(frozen=True)
class LoaderLimits:
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_records_per_file: int = DEFAULT_MAX_RECORDS_PER_FILE


@dataclass(frozen=True)
class LoadedDataset:
    root: Path
    manifest: DatasetManifest
    dataset_id: str
    dataset_version: str
    schema_version: str
    rubric_version: str
    sanitizer_version: str
    data_classification: str
    dataset_fingerprint: str
    roleplay_cases: tuple[GoldenCase, ...] = ()
    memory_cases: tuple[GoldenCase, ...] = ()
    pairwise_cases: tuple[PairwiseCase, ...] = ()
    annotations: tuple[Annotation, ...] = ()
    consolidation_batches: tuple[ConsolidationBatch, ...] = ()
    realtime_scenarios: tuple[RealtimeScenario, ...] = ()
    file_fingerprints: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def base_cases(self) -> dict[str, GoldenCase]:
        cases = {c.case_id: c for c in self.roleplay_cases}
        cases.update({c.case_id: c for c in self.memory_cases})
        return cases

    @property
    def text_case_count(self) -> int:
        return len(self.roleplay_cases) + len(self.memory_cases)

    def cases_for_split(self, split: str) -> tuple[GoldenCase, ...]:
        return tuple(
            c
            for c in (*self.roleplay_cases, *self.memory_cases)
            if c.split == split
        )

    def pairwise_for_split(self, split: str) -> tuple[PairwiseCase, ...]:
        return tuple(c for c in self.pairwise_cases if c.split == split)


class DatasetLoader:
    def __init__(self, root: Path | str, *, limits: LoaderLimits | None = None) -> None:
        self._root = Path(root)
        self._limits = limits or LoaderLimits()

    # -- public ---------------------------------------------------------

    def load(self) -> LoadedDataset:
        if not self._root.is_dir():
            raise DatasetLoadError(f"dataset root is not a directory: {self._root.name}")

        manifest_raw = self._read_manifest_raw()
        manifest = self._parse_manifest(manifest_raw)
        self._verify_dataset_fingerprint(manifest_raw)

        files_meta = manifest.files
        warnings: list[str] = []
        raw: dict[str, list[dict[str, Any]]] = {}
        file_fingerprints: dict[str, str] = {}
        total_bytes = 0

        for kind in DATASET_FILE_KINDS:
            entry = files_meta.get(kind)
            if entry is None:
                warnings.append(f"manifest lists no '{kind}' file; treated as empty")
                raw[kind] = []
                continue
            path = self._root / entry.path
            if not path.exists():
                raise DatasetLoadError(
                    f"manifest declares '{kind}' file but it is absent on disk"
                )
            file_bytes = path.read_bytes()
            total_bytes += len(file_bytes)
            if total_bytes > self._limits.max_total_bytes:
                raise DatasetLoadError("dataset exceeds max_total_bytes limit")

            actual_sha = sha256_file(path)
            if actual_sha != entry.sha256:
                raise DatasetLoadError(f"sha256 mismatch for dataset file kind '{kind}'")
            file_fingerprints[kind] = actual_sha

            if kind in JSONL_FILE_KINDS:
                raw[kind] = self._read_jsonl(path, kind, file_bytes)
            else:
                raw[kind] = self._read_realtime_yaml(path, file_bytes)

        # pairwise base resolution requires roleplay/memory fingerprints
        base_fingerprints = {
            str(row["case_id"]): base_context_fingerprint(row)
            for kind in ("roleplay", "memory")
            for row in raw[kind]
            if isinstance(row.get("case_id"), str)
        }

        relation_errors = validate_relations(
            roleplay_rows=raw["roleplay"],
            memory_rows=raw["memory"],
            pairwise_rows=raw["pairwise"],
            annotation_rows=raw["annotations"],
            consolidation_rows=raw["consolidation"],
            base_fingerprints=base_fingerprints,
            expected_dataset_id=manifest.dataset_id,
            expected_dataset_version=manifest.dataset_version,
            expected_rubric_version=manifest.rubric_version,
        )
        if relation_errors:
            raise DatasetLoadError(
                "dataset relational validation failed: "
                + "; ".join(relation_errors[:10])
                + (f" (+{len(relation_errors) - 10} more)" if len(relation_errors) > 10 else "")
            )

        roleplay = self._parse_models(raw["roleplay"], GoldenCase, "roleplay")
        memory = self._parse_models(raw["memory"], GoldenCase, "memory")
        pairwise = self._parse_models(raw["pairwise"], PairwiseCase, "pairwise")
        annotations = self._parse_models(raw["annotations"], Annotation, "annotations")
        consolidation = self._parse_models(
            raw["consolidation"], ConsolidationBatch, "consolidation"
        )
        scenarios = self._parse_models(raw["realtime"], RealtimeScenario, "realtime")

        return LoadedDataset(
            root=self._root,
            manifest=manifest,
            dataset_id=manifest.dataset_id,
            dataset_version=manifest.dataset_version,
            schema_version=manifest.schema_version,
            rubric_version=manifest.rubric_version,
            sanitizer_version=manifest.sanitizer_version,
            data_classification=manifest.data_classification,
            dataset_fingerprint=manifest.fingerprint,
            roleplay_cases=tuple(sorted(roleplay, key=lambda c: c.case_id)),
            memory_cases=tuple(sorted(memory, key=lambda c: c.case_id)),
            pairwise_cases=tuple(sorted(pairwise, key=lambda c: c.case_id)),
            annotations=tuple(sorted(annotations, key=lambda a: a.annotation_id)),
            consolidation_batches=tuple(sorted(consolidation, key=lambda b: b.batch_id)),
            realtime_scenarios=tuple(sorted(scenarios, key=lambda s: s.scenario_id)),
            file_fingerprints=file_fingerprints,
            warnings=tuple(warnings),
        )

    # -- internals ------------------------------------------------------

    def _read_manifest_raw(self) -> dict[str, Any]:
        manifest_path = self._root / "manifest.json"
        if not manifest_path.exists():
            raise DatasetLoadError("manifest.json missing; dataset identity cannot be resolved")
        try:
            raw_bytes = manifest_path.read_bytes()
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DatasetLoadError("manifest.json is not valid UTF-8") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DatasetLoadError(f"manifest.json invalid JSON at line {exc.lineno}") from exc
        if not isinstance(data, dict):
            raise DatasetLoadError("manifest.json must be a JSON object")
        return data

    def _parse_manifest(self, raw: dict[str, Any]) -> DatasetManifest:
        try:
            return DatasetManifest.model_validate(raw)
        except ValidationError as exc:
            raise DatasetLoadError(
                f"manifest.json failed schema validation: {_first_error_loc(exc)}"
            ) from exc

    def _verify_dataset_fingerprint(self, raw: dict[str, Any]) -> None:
        declared = raw.get("fingerprint")
        recomputed = dataset_fingerprint_from_manifest(raw)
        if declared != recomputed:
            raise DatasetLoadError("dataset fingerprint stale or tampered (manifest mismatch)")

    def _read_jsonl(self, path: Path, kind: str, file_bytes: bytes) -> list[dict[str, Any]]:
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DatasetLoadError(f"{kind}: file is not valid UTF-8") from exc
        rows: list[dict[str, Any]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if len(rows) >= self._limits.max_records_per_file:
                raise DatasetLoadError(f"{kind}: exceeds max_records_per_file limit")
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise DatasetLoadError(f"{kind}:{line_no} invalid JSON") from exc
            if not isinstance(obj, dict):
                raise DatasetLoadError(f"{kind}:{line_no} expected JSON object")
            rows.append(obj)
        return rows

    def _read_realtime_yaml(self, path: Path, file_bytes: bytes) -> list[dict[str, Any]]:
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DatasetLoadError("realtime: file is not valid UTF-8") from exc
        try:
            docs = [doc for doc in yaml.safe_load_all(text) if doc]
        except yaml.YAMLError as exc:
            raise DatasetLoadError(f"realtime: invalid YAML: {type(exc).__name__}") from exc
        rows: list[dict[str, Any]] = []
        for doc in docs:
            if not isinstance(doc, dict):
                raise DatasetLoadError("realtime: scenario document must be a mapping")
            rows.append(doc)
        if len(rows) > self._limits.max_records_per_file:
            raise DatasetLoadError("realtime: exceeds max_records_per_file limit")
        return rows

    def _parse_models(
        self, rows: list[dict[str, Any]], model: type[BaseModel], kind: str
    ) -> list[BaseModel]:
        parsed: list[BaseModel] = []
        for index, row in enumerate(rows, start=1):
            try:
                parsed.append(model.model_validate(row))
            except ValidationError as exc:
                identifier = (
                    row.get("case_id")
                    or row.get("annotation_id")
                    or row.get("batch_id")
                    or row.get("scenario_id")
                    or "<no-id>"
                )
                raise DatasetLoadError(
                    f"{kind}:{index} ({identifier}) failed schema validation: "
                    f"{_first_error_loc(exc)}"
                ) from exc
        return parsed


def _first_error_loc(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "unknown field"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
    error_type = str(first.get("type", "invalid"))
    return f"{loc} ({error_type})"
