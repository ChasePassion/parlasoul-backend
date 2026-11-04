"""Immutable evaluation run configuration.

Maps the already-loaded core ``Settings`` into a frozen allowlist; never reads
``.env`` a second time and never invents default providers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.core.config import Settings
from src.evaluation.constants import ContentMode
from src.evaluation.errors import EvalConfigError

_CONTENT_MODES = ("none", "redacted", "full")


@dataclass(frozen=True)
class EvalConfig:
    environment: str
    dataset_path: Path | None
    results_dir: Path
    report_allowlist_dirs: tuple[Path, ...]
    judge_provider: str | None
    judge_model: str | None
    rubric_version: str
    prompt_template_version: str
    concurrency: int
    generation_timeout_s: float
    judge_timeout_s: float
    content_mode: ContentMode
    retention_days: int
    langfuse_enabled: bool
    capture_input: bool
    capture_output: bool

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


def build_eval_config(
    settings: Settings,
    *,
    dataset_path: str | None = None,
    results_dir: str | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
    concurrency: int | None = None,
    content_mode: str | None = None,
    retention_days: int | None = None,
) -> EvalConfig:
    """Validate and freeze the evaluation configuration.

    CLI overrides win over Settings; every value is checked fail-closed.
    """
    resolved_content_mode = content_mode or settings.EVAL_CONTENT_MODE
    if resolved_content_mode not in _CONTENT_MODES:
        raise EvalConfigError(f"invalid content mode (allowed: {', '.join(_CONTENT_MODES)})")

    environment = settings.environment_name
    if environment == "production" and resolved_content_mode == "full":
        raise EvalConfigError("content_mode=full is rejected in production")

    resolved_concurrency = concurrency if concurrency is not None else settings.EVAL_CONCURRENCY
    if not isinstance(resolved_concurrency, int) or resolved_concurrency <= 0:
        raise EvalConfigError("concurrency must be a positive integer")

    if settings.EVAL_GENERATION_TIMEOUT_SECONDS <= 0 or settings.EVAL_JUDGE_TIMEOUT_SECONDS <= 0:
        raise EvalConfigError("evaluation timeouts must be positive")

    resolved_retention = retention_days if retention_days is not None else settings.EVAL_RETENTION_DAYS
    if not isinstance(resolved_retention, int) or resolved_retention <= 0:
        raise EvalConfigError("retention days must be a positive integer")

    allowlist_dirs = tuple(
        Path(chunk.strip())
        for chunk in settings.EVAL_REPORT_ALLOWLIST_DIRS.split(",")
        if chunk.strip()
    )
    if not allowlist_dirs:
        raise EvalConfigError("report allowlist directories must not be empty")

    resolved_dataset_path = dataset_path if dataset_path is not None else settings.EVAL_DATASET_PATH

    return EvalConfig(
        environment=environment,
        dataset_path=Path(resolved_dataset_path) if resolved_dataset_path else None,
        results_dir=Path(results_dir or settings.EVAL_RESULTS_DIR),
        report_allowlist_dirs=allowlist_dirs,
        judge_provider=judge_provider if judge_provider is not None else settings.EVAL_JUDGE_PROVIDER,
        judge_model=judge_model if judge_model is not None else settings.EVAL_JUDGE_MODEL,
        rubric_version=settings.EVAL_RUBRIC_VERSION,
        prompt_template_version=settings.EVAL_PROMPT_TEMPLATE_VERSION,
        concurrency=resolved_concurrency,
        generation_timeout_s=settings.EVAL_GENERATION_TIMEOUT_SECONDS,
        judge_timeout_s=settings.EVAL_JUDGE_TIMEOUT_SECONDS,
        content_mode=resolved_content_mode,  # type: ignore[arg-type]
        retention_days=resolved_retention,
        langfuse_enabled=settings.EVAL_LANGFUSE_ENABLED,
        capture_input=settings.EVAL_CAPTURE_INPUT,
        capture_output=settings.EVAL_CAPTURE_OUTPUT,
    )


def assert_report_path_allowed(out_path: Path, config: EvalConfig) -> Path:
    """Reports may only be written inside allowlisted directories."""
    resolved = out_path.resolve()
    for allowed in config.report_allowlist_dirs:
        allowed_resolved = allowed.resolve()
        if resolved == allowed_resolved or allowed_resolved in resolved.parents:
            return resolved
    raise EvalConfigError("report output path is outside the allowlisted directories")


def assert_dimensions_allowed(dimensions: Sequence[str], allowed: frozenset[str]) -> None:
    for dimension in dimensions:
        if dimension not in allowed:
            raise EvalConfigError(f"unknown evaluation dimension: {dimension}")
