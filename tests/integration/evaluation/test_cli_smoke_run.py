"""End-to-end CLI smoke run: mock gateway, eval tables in an isolated schema.

Acceptance #2: one eval_runs row + one unique eval_results row per case,
judge/route metadata persisted, content_mode=none keeps raw text out of the
database. Works on pristine CI databases (no migrations) because all tables
are created inside a per-test schema.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import text as sql_text
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.config import settings
from src.db.models import (
    Candidate,
    Character,
    Chat,
    EvalResult,
    EvalRun,
    Turn,
    User,
)
from src.evaluation.storage.db_writer import DbWriter
from src.services.llm_gateway_service import LLMGatewayService
from tests.unit.evaluation.fixture_factory import make_roleplay_case, write_dataset

_TABLES = [
    User.__table__,
    Character.__table__,
    Chat.__table__,
    Turn.__table__,
    Candidate.__table__,
    EvalRun.__table__,
    EvalResult.__table__,
]


async def _fake_stream_text(self, *, route, messages, operation, **kwargs):
    yield "A calm, in-character reply."


async def _fake_generate_text(self, *, route, system_prompt, user_message, operation, **kwargs):
    if operation == "eval.judge.score":
        return json.dumps({"score": 4, "rationale": "consistent with persona"})
    if operation == "eval.judge.ooc":
        return json.dumps({"ooc": False, "rationale": "stays in character"})
    if operation == "eval.judge.compare":
        return json.dumps({"winner": "response_1", "rationale": "more engaging"})
    if operation == "eval.judge.claims":
        return json.dumps({"supported_claims": [], "rationale": "none"})
    if operation == "eval.judge.abstention":
        return json.dumps({"abstained": True, "rationale": "admits unknown"})
    return json.dumps({})


@pytest.fixture()
def smoke_dataset(tmp_path: Path) -> Path:
    root = tmp_path / "dataset"
    write_dataset(
        root,
        roleplay=[make_roleplay_case("smoke-rp-1")],
        memory=[],
        pairwise=[],
        annotations=[],
        consolidation=[],
        realtime_scenarios=[],
    )
    return root


@pytest.fixture()
def cli_smoke_env(ensure_pg_enum_types):
    """Isolated schema with all tables the CLI may touch.

    Each phase (setup / CLI run / verify / teardown) creates and disposes its
    own engine so no connection pool is reused across event loops.
    """
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        pytest.skip("requires postgresql+asyncpg DATABASE_URL")

    schema = f"test_cli_smoke_{uuid.uuid4().hex}"
    quoted = '"' + schema + '"'
    connect_args = dict(settings.database_connect_args)
    server_settings = dict(connect_args.get("server_settings", {}))
    server_settings["search_path"] = f"{schema},public"
    connect_args["server_settings"] = server_settings

    async def _setup() -> None:
        admin = create_async_engine(
            settings.DATABASE_URL, echo=False, connect_args=settings.database_connect_args
        )
        engine = create_async_engine(
            settings.DATABASE_URL, echo=False, connect_args=connect_args
        )
        try:
            async with admin.begin() as conn:
                await conn.execute(sql_text(f"CREATE SCHEMA {quoted}"))
            async with engine.begin() as conn:
                await ensure_pg_enum_types(conn)
                await conn.run_sync(SQLModel.metadata.create_all, tables=_TABLES)
        finally:
            await engine.dispose()
            await admin.dispose()

    asyncio.run(_setup())
    yield {"schema": schema, "quoted": quoted, "connect_args": connect_args}

    async def _teardown() -> None:
        admin = create_async_engine(
            settings.DATABASE_URL, echo=False, connect_args=settings.database_connect_args
        )
        try:
            async with admin.begin() as conn:
                await conn.execute(sql_text(f"DROP SCHEMA IF EXISTS {quoted} CASCADE"))
        finally:
            await admin.dispose()

    asyncio.run(_teardown())


def test_cli_case_run_end_to_end(
    smoke_dataset: Path,
    cli_smoke_env: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(LLMGatewayService, "stream_text", _fake_stream_text)
    monkeypatch.setattr(LLMGatewayService, "generate_text", _fake_generate_text)

    # Give the CLI its own session factory bound to the isolated schema.
    import src.evaluation.storage.db_writer as db_writer_mod

    cli_engine = create_async_engine(
        settings.DATABASE_URL, echo=False, connect_args=cli_smoke_env["connect_args"]
    )
    cli_factory = async_sessionmaker(
        bind=cli_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    monkeypatch.setattr(db_writer_mod, "AsyncSessionLocal", cli_factory)

    run_name = f"cli smoke test {uuid.uuid4().hex[:8]}"

    from scripts import run_eval

    # Stub route resolution so the smoke run never depends on real provider
    # keys being configured (CI has none); the gateway calls themselves are
    # already stubbed above.
    class _StubRoute:
        provider = "stubjudge"
        model = "stub-judge-model"
        sdk_kind = "openai_sdk"
        api_key = "stub-key"
        api_base = None
        model_type_label = "stubjudge:stub-judge-model"

    class _StubResolver:
        def resolve(self, *, provider, model):
            return _StubRoute()

    monkeypatch.setattr(run_eval, "JudgeRouteResolver", _StubResolver)
    monkeypatch.setattr(run_eval, "resolve_generation_route", lambda: _StubRoute())

    exit_code = run_eval.main(
        [
            "--dataset",
            str(smoke_dataset),
            "--split",
            "smoke",
            "--judge-provider",
            "deepseek",
            "--judge-model",
            "deepseek-v4-flash",
            "--content-mode",
            "none",
            "--name",
            run_name,
        ]
    )
    assert exit_code == 0

    async def _verify() -> dict:
        # Fresh engine: the CLI's asyncio.run already closed its own loop.
        verify_engine = create_async_engine(
            settings.DATABASE_URL, echo=False, connect_args=cli_smoke_env["connect_args"]
        )
        verify_factory = async_sessionmaker(
            bind=verify_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        writer = DbWriter(verify_factory)
        async with writer._system_session() as session:  # noqa: SLF001
            runs = (
                await session.execute(select(EvalRun).where(EvalRun.name == run_name))
            ).scalars().all()
            assert len(runs) == 1
            run = runs[0]
            assert run.status == "completed"
            assert run.run_kind == "case"
            assert run.case_count == 1
            assert run.completed_case_count == 1
            assert run.effective_judge_provider == "stubjudge"
            assert run.rubric_version == "parlasoul-roleplay-5d-ooc-v1"
            results = (
                await session.execute(
                    select(EvalResult).where(EvalResult.run_id == run.id)
                )
            ).scalars().all()
            assert len(results) == 1
            result = results[0]
            assert result.status == "completed"
            assert result.response_status == "valid"
            assert result.content_mode == "none"
            assert result.input_text is None
            assert result.actual_output_text is None
            assert result.generation_provider is not None
            assert result.judge_provider == "stubjudge"
            assert result.dimension_scores_json.get("persona_fidelity") == 4
            summary = dict(run.summary_json)
        await verify_engine.dispose()
        return summary

    summary = asyncio.run(_verify())
    assert summary["dimensions"]["persona_fidelity"]["sample_count"] == 1


def test_cli_dry_run_empty_dataset(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    write_dataset(
        root,
        roleplay=[],
        memory=[],
        pairwise=[],
        annotations=[],
        consolidation=[],
        realtime_scenarios=[],
    )

    from scripts import run_eval

    exit_code = run_eval.main(["--dataset", str(root), "--dry-run", "--split", "smoke"])
    assert exit_code == 0


def test_cli_rejects_unknown_dimension(tmp_path: Path) -> None:
    from scripts import run_eval

    exit_code = run_eval.main(
        ["--dataset", str(tmp_path), "--dry-run", "--dimensions", "style_flair"]
    )
    assert exit_code == 2


def test_cli_invalid_dataset_path_fails_closed(tmp_path: Path) -> None:
    from scripts import run_eval

    exit_code = run_eval.main(["--dataset", str(tmp_path / "missing"), "--dry-run"])
    assert exit_code == 2
