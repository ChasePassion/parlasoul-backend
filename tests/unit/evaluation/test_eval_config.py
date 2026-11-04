"""EvalConfig validation: fail-closed gates for modes, capture, paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import Settings
from src.evaluation.config import assert_report_path_allowed, build_eval_config
from src.evaluation.errors import EvalConfigError

_REQUIRED = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "SECRET_KEY": "test-secret",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USER": "smtp-user",
    "SMTP_PASSWORD": "smtp-pass",
    "EMBEDDING_API_KEY": "emb-key",
    "MILVUS_URL": "http://localhost:19530",
}


def _settings(**overrides: str) -> Settings:
    return Settings(**{**_REQUIRED, **overrides}, _env_file=None)


def test_defaults_are_safe() -> None:
    config = build_eval_config(_settings())

    assert config.content_mode == "none"
    assert config.concurrency > 0
    # observability capture defaults on so Judge prompts/replies stay inspectable
    assert config.capture_input is True
    assert config.capture_output is True
    assert config.is_production is False


def test_capture_flags_can_be_disabled() -> None:
    config = build_eval_config(
        _settings(EVAL_CAPTURE_INPUT="false", EVAL_CAPTURE_OUTPUT="false")
    )
    assert config.capture_input is False
    assert config.capture_output is False


def test_production_rejects_full_content_mode() -> None:
    settings = _settings(ENVIRONMENT="production")
    with pytest.raises(EvalConfigError, match="full is rejected"):
        build_eval_config(settings, content_mode="full")


def test_unknown_environment_is_not_development() -> None:
    config = build_eval_config(_settings(ENVIRONMENT="staging"))
    assert config.environment == "staging"
    assert config.is_production is False


def test_invalid_content_mode_rejected() -> None:
    with pytest.raises(EvalConfigError, match="content mode"):
        build_eval_config(_settings(), content_mode="raw")


def test_non_positive_concurrency_rejected() -> None:
    with pytest.raises(EvalConfigError, match="concurrency"):
        build_eval_config(_settings(), concurrency=0)


def test_report_path_allowlist(tmp_path: Path) -> None:
    results = tmp_path / "data" / "eval" / "results"
    settings = _settings(EVAL_REPORT_ALLOWLIST_DIRS=str(results))
    config = build_eval_config(settings, results_dir=str(results))

    allowed = assert_report_path_allowed(results / "run.jsonl", config)
    assert allowed.is_relative_to(results)

    with pytest.raises(EvalConfigError, match="allowlist"):
        assert_report_path_allowed(tmp_path / "elsewhere" / "report.md", config)
