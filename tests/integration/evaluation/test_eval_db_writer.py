"""Integration tests: eval_runs/eval_results constraints, idempotent writer,
system-only RLS. Uses an isolated per-test schema (no migrations), replicating
the migration's RLS statements.

RLS enforcement is verified through a temporary low-privilege role
(``SET ROLE``): superuser/owner sessions bypass RLS by design, so the dev
database's admin role cannot prove the policies work.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import select
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

_TABLES = [
    User.__table__,
    Character.__table__,
    Chat.__table__,
    Turn.__table__,
    Candidate.__table__,
    EvalRun.__table__,
    EvalResult.__table__,
]

_RLS_TABLES = ("eval_runs", "eval_results")


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@pytest.fixture()
async def eval_pg_env(ensure_pg_enum_types) -> AsyncIterator[dict[str, Any]]:
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        pytest.skip("PostgreSQL integration test requires postgresql+asyncpg DATABASE_URL.")

    schema_name = f"test_eval_{uuid.uuid4().hex}"
    quoted_schema = _quote_ident(schema_name)
    test_connect_args = dict(settings.database_connect_args)
    server_settings = dict(test_connect_args.get("server_settings", {}))
    server_settings["search_path"] = f"{schema_name},public"
    test_connect_args["server_settings"] = server_settings

    admin_engine = create_async_engine(
        settings.DATABASE_URL, echo=False, connect_args=settings.database_connect_args
    )
    test_engine = create_async_engine(
        settings.DATABASE_URL, echo=False, connect_args=test_connect_args
    )

    # Remap tables into the isolated schema explicitly; unqualified
    # create_all would see the migrated public tables via search_path and
    # skip creation (or worse, alter the real tables).
    isolated_meta = MetaData()
    isolated_tables = [t.tometadata(isolated_meta, schema=schema_name) for t in _TABLES]

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))

        async with test_engine.begin() as conn:
            await ensure_pg_enum_types(conn)
            await conn.run_sync(isolated_meta.create_all, tables=isolated_tables)
            for table in _RLS_TABLES:
                qualified = f"{quoted_schema}.{table}"
                await conn.execute(text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
                await conn.execute(text(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"))
                await conn.execute(
                    text(
                        f"""
                        CREATE POLICY {table}_system_only ON {qualified}
                          FOR ALL
                          USING (current_setting('app.current_user_id', true) = 'system')
                          WITH CHECK (current_setting('app.current_user_id', true) = 'system')
                        """
                    )
                )

        session_factory = async_sessionmaker(
            bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        yield {
            "schema_name": schema_name,
            "quoted_schema": quoted_schema,
            "session_factory": session_factory,
            "admin_engine": admin_engine,
        }
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        await test_engine.dispose()
        await admin_engine.dispose()


@pytest.fixture()
async def eval_pg(eval_pg_env) -> AsyncIterator[async_sessionmaker]:
    yield eval_pg_env["session_factory"]


@pytest.fixture()
async def restricted_role(eval_pg_env) -> AsyncIterator[str]:
    """Temporary low-privilege role to prove RLS (superusers bypass RLS)."""
    role_name = f"eval_rls_probe_{uuid.uuid4().hex[:12]}"
    quoted_role = _quote_ident(role_name)
    quoted_schema = eval_pg_env["quoted_schema"]
    admin_engine = eval_pg_env["admin_engine"]

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {quoted_role} NOLOGIN"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}"))
            for table in _RLS_TABLES:
                await conn.execute(
                    text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {quoted_schema}.{table} TO {quoted_role}")
                )
            for table in ("users", "characters", "candidates"):
                await conn.execute(
                    text(f"GRANT REFERENCES ON {quoted_schema}.{table} TO {quoted_role}")
                )
    except DBAPIError as exc:
        pytest.skip(f"cannot create probe role in this environment: {exc.__class__.__name__}")

    yield role_name

    async with admin_engine.begin() as conn:
        await conn.execute(text(f"DROP OWNED BY {quoted_role}"))
        await conn.execute(text(f"DROP ROLE IF EXISTS {quoted_role}"))


def _run(**overrides) -> EvalRun:
    base = {
        "name": "smoke run",
        "dataset_id": "parlasoul_gold",
        "dataset_version": "v1.0.0",
        "dataset_fingerprint": "a" * 64,
        "runner_version": "parlasoul-eval-runner-0.1.0",
        "run_kind": "case",
        "judge_route": "locked",
        "effective_judge_provider": "deepseek",
        "effective_judge_model": "deepseek-v4-flash",
        "rubric_version": "parlasoul-roleplay-5d-ooc-v1",
        "prompt_template_version": "parlasoul-judge-prompt-v1",
        "prompt_hash": "b" * 64,
        "content_mode": "none",
        "status": "running",
    }
    base.update(overrides)
    return EvalRun(**base)


def _result(run_id: uuid.UUID, case_id: str = "case-1", **overrides) -> EvalResult:
    base = {
        "run_id": run_id,
        "case_id": case_id,
        "runner": "chat_replay",
        "status": "completed",
        "response_status": "valid",
        "content_mode": "none",
        "input_ref": f"case:{case_id}",
        "output_ref": f"case:{case_id}",
    }
    base.update(overrides)
    return EvalResult(**base)


async def _select_result(session_factory, run_id: uuid.UUID, case_id: str) -> EvalResult | None:
    writer = DbWriter(session_factory)
    async with writer._system_session() as session:  # noqa: SLF001 - test access
        rows = await session.execute(
            select(EvalResult).where(
                EvalResult.run_id == run_id, EvalResult.case_id == case_id
            )
        )
        return rows.scalars().first()


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_bumps_attempt(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run()
    await writer.create_run(run)

    await writer.upsert_result(_result(run.id, status="cancelled", response_status="cancelled"))
    await writer.upsert_result(_result(run.id, status="completed", response_status="valid"))

    stored = await _select_result(eval_pg, run.id, "case-1")
    assert stored is not None
    assert stored.status == "completed"
    assert stored.attempt == 2


@pytest.mark.asyncio
async def test_mark_cases_cancelled_protects_completed(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run()
    await writer.create_run(run)
    await writer.upsert_result(_result(run.id, case_id="done-case"))

    written = await writer.mark_cases_cancelled(
        run_id=run.id,
        runner="chat_replay",
        case_ids=["done-case", "queued-case"],
        content_mode="none",
    )
    assert written == 2

    done = await _select_result(eval_pg, run.id, "done-case")
    queued = await _select_result(eval_pg, run.id, "queued-case")
    assert done is not None and done.status == "completed"
    assert queued is not None and queued.status == "cancelled"
    assert queued.response_status == "cancelled"


@pytest.mark.asyncio
async def test_finalize_run_derives_counts_and_status(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run()
    await writer.create_run(run)
    await writer.upsert_result(_result(run.id, case_id="c1"))
    await writer.upsert_result(
        _result(run.id, case_id="c2", status="failed", response_status="judge_error")
    )

    await writer.finalize_run(
        run.id, status="partial", run_kind="case", summary_json={"ok": 1}
    )

    stored = await writer.get_run(run.id)
    assert stored is not None
    assert stored.status == "partial"
    assert stored.completed_case_count == 1
    assert stored.failed_case_count == 1
    assert stored.summary_json == {"ok": 1}
    assert stored.finished_at is not None


@pytest.mark.asyncio
async def test_rls_policy_metadata(eval_pg_env) -> None:
    """Policies exist and RLS is enabled+forced (works even as superuser)."""
    async with eval_pg_env["admin_engine"].begin() as conn:
        policies = (
            await conn.execute(
                text(
                    "SELECT tablename, policyname FROM pg_policies "
                    "WHERE schemaname = :schema ORDER BY tablename"
                ),
                {"schema": eval_pg_env["schema_name"]},
            )
        ).all()
        flags = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema AND c.relname IN ('eval_runs', 'eval_results')"
                ),
                {"schema": eval_pg_env["schema_name"]},
            )
        ).all()

    assert {tuple(row) for row in policies} == {
        ("eval_results", "eval_results_system_only"),
        ("eval_runs", "eval_runs_system_only"),
    }
    for row in flags:
        assert row[1] is True and row[2] is True


@pytest.mark.asyncio
async def test_rls_blocks_non_system_context(eval_pg, restricted_role) -> None:
    writer = DbWriter(eval_pg)
    run = _run()
    await writer.create_run(run)

    quoted_role = _quote_ident(restricted_role)

    # non-system context under a non-superuser role: invisible + unwritable
    async with eval_pg() as session:
        session.info["current_user_id"] = str(uuid.uuid4())
        await session.execute(text(f"SET ROLE {quoted_role}"))
        rows = await session.execute(select(EvalRun))
        assert rows.scalars().all() == []
        session.add(_run())
        with pytest.raises(DBAPIError):
            await session.commit()

    # empty context (API default) is equally blocked
    async with eval_pg() as session:
        session.info["current_user_id"] = ""
        await session.execute(text(f"SET ROLE {quoted_role}"))
        rows = await session.execute(select(EvalRun))
        assert rows.scalars().all() == []

    # system context under the same role sees and writes
    async with eval_pg() as session:
        session.info["current_user_id"] = "system"
        await session.execute(text(f"SET ROLE {quoted_role}"))
        rows = await session.execute(select(EvalRun))
        assert len(rows.scalars().all()) == 1
        session.add(_run(name="system-visible run"))
        await session.commit()

    async with eval_pg() as session:
        session.info["current_user_id"] = "system"
        await session.execute(text(f"SET ROLE {quoted_role}"))
        rows = await session.execute(select(EvalRun))
        assert len(rows.scalars().all()) == 2


@pytest.mark.asyncio
async def test_content_mode_none_rejects_raw_text_columns(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run()
    await writer.create_run(run)

    bad = _result(run.id, case_id="c-text", input_text="raw user text")
    with pytest.raises(IntegrityError):
        await writer.upsert_result(bad)


@pytest.mark.asyncio
async def test_invalid_status_rejected(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run(status="finished")  # not in enum
    with pytest.raises(IntegrityError):
        await writer.create_run(run)


@pytest.mark.asyncio
async def test_aggregate_run_cannot_carry_case_counts(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run(
        run_kind="aggregate",
        judge_route="none",
        effective_judge_provider=None,
        effective_judge_model=None,
        rubric_version=None,
        prompt_hash=None,
        case_count=3,
    )
    with pytest.raises(IntegrityError):
        await writer.create_run(run)


@pytest.mark.asyncio
async def test_locked_judge_route_requires_fields(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run(judge_route="locked", prompt_hash=None)
    with pytest.raises(IntegrityError):
        await writer.create_run(run)


@pytest.mark.asyncio
async def test_pairwise_fields_restricted_to_pairwise_runner(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run()
    await writer.create_run(run)

    bad = _result(run.id, case_id="c-pw", pairwise_winner="a")
    with pytest.raises(IntegrityError):
        await writer.upsert_result(bad)

    ok = _result(
        run.id,
        case_id="c-pw-ok",
        runner="pairwise_replay",
        pairwise_winner="tie",
        swap_consistent=True,
        variant_a_ref="synthetic.variant.a",
        variant_b_ref="synthetic.variant.b",
    )
    await writer.upsert_result(ok)
    stored = await _select_result(eval_pg, run.id, "c-pw-ok")
    assert stored is not None and stored.pairwise_winner == "tie"


@pytest.mark.asyncio
async def test_duplicate_result_conflict_only_on_same_runner(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run()
    await writer.create_run(run)

    await writer.upsert_result(_result(run.id, case_id="c-dup", runner="chat_replay"))
    await writer.upsert_result(_result(run.id, case_id="c-dup", runner="candidate_replay"))

    async with writer._system_session() as session:  # noqa: SLF001
        rows = await session.execute(
            select(EvalResult).where(EvalResult.case_id == "c-dup")
        )
        assert len(rows.scalars().all()) == 2


@pytest.mark.asyncio
async def test_delete_run_cascades_results(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run()
    await writer.create_run(run)
    await writer.upsert_result(_result(run.id))

    await writer.delete_run(run.id)

    assert await writer.get_run(run.id) is None
    assert await _select_result(eval_pg, run.id, "case-1") is None


@pytest.mark.asyncio
async def test_rationale_length_check(eval_pg) -> None:
    writer = DbWriter(eval_pg)
    run = _run(content_mode="redacted")
    await writer.create_run(run)

    bad = _result(run.id, case_id="c-long", judge_rationale="x" * 4001)
    with pytest.raises(IntegrityError):
        await writer.upsert_result(bad)
