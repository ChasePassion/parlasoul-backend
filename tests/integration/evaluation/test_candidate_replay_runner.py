"""CandidateReplayRunner: offline scoring of stored production candidates."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import text as sql_text
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.config import settings
from src.db.models import Candidate, Character, Chat, Turn, User
from src.evaluation.datasets.schema import GoldenCase
from src.evaluation.runners.base import CaseExecutionContext
from src.evaluation.runners.candidate_runner import CandidateReplayRunner
from tests.unit.evaluation.fixture_factory import make_roleplay_case
from tests.unit.evaluation.test_runners_pipeline import StubJudge

_TABLES = [
    User.__table__,
    Character.__table__,
    Chat.__table__,
    Turn.__table__,
    Candidate.__table__,
]


@pytest.fixture()
async def candidate_env(ensure_pg_enum_types) -> AsyncIterator[dict]:
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        pytest.skip("requires postgresql+asyncpg DATABASE_URL")

    schema = f"test_cand_{uuid.uuid4().hex}"
    quoted = '"' + schema + '"'
    connect_args = dict(settings.database_connect_args)
    server_settings = dict(connect_args.get("server_settings", {}))
    server_settings["search_path"] = f"{schema},public"
    connect_args["server_settings"] = server_settings

    engine = create_async_engine(settings.DATABASE_URL, echo=False, connect_args=connect_args)
    admin_engine = create_async_engine(
        settings.DATABASE_URL, echo=False, connect_args=settings.database_connect_args
    )
    isolated_meta = MetaData()
    isolated_tables = [t.tometadata(isolated_meta, schema=schema) for t in _TABLES]

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(sql_text(f"CREATE SCHEMA {quoted}"))
        async with engine.begin() as conn:
            await ensure_pg_enum_types(conn)
            await conn.run_sync(isolated_meta.create_all, tables=isolated_tables)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        ids = await _seed(factory)
        yield {"factory": factory, **ids}
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(sql_text(f"DROP SCHEMA IF EXISTS {quoted} CASCADE"))
        await admin_engine.dispose()


async def _seed(factory: async_sessionmaker) -> dict:
    now = datetime.now(UTC)
    user_id = uuid.uuid4()
    character_id = uuid.uuid4()
    chat_id = uuid.uuid4()

    user_turn_id = uuid.uuid4()
    user_cand_id = uuid.uuid4()
    assistant_turn_id = uuid.uuid4()
    good_cand_id = uuid.uuid4()
    error_cand_turn_id = uuid.uuid4()
    error_cand_id = uuid.uuid4()
    draft_turn_id = uuid.uuid4()
    draft_cand_id = uuid.uuid4()
    greeting_turn_id = uuid.uuid4()
    greeting_cand_id = uuid.uuid4()

    async with factory() as session:
        session.add_all(
            [
                User(id=user_id, email=f"cand-{uuid.uuid4().hex}@example.com"),
                Character(
                    id=character_id,
                    creator_id=user_id,
                    name="Cand Char",
                    description="candidate replay fixture",
                    visibility="PUBLIC",
                    system_prompt="stay in character",
                    greeting_message="hi",
                ),
                Chat(
                    id=chat_id,
                    user_id=user_id,
                    character_id=character_id,
                    type="ONE_ON_ONE",
                    state="ACTIVE",
                    visibility="PRIVATE",
                    origin="user",
                    title="t",
                ),
            ]
        )
        await session.commit()

        turns = [
            Turn(
                id=user_turn_id, chat_id=chat_id, turn_no=1, author_type="USER",
                state="OK", author_user_id=user_id, primary_candidate_id=None,
                created_at=now,
            ),
            Turn(
                id=assistant_turn_id, chat_id=chat_id, turn_no=2, author_type="CHARACTER",
                state="OK", author_character_id=character_id, primary_candidate_id=None,
                parent_turn_id=user_turn_id, created_at=now,
            ),
            Turn(
                id=error_cand_turn_id, chat_id=chat_id, turn_no=3, author_type="CHARACTER",
                state="OK", author_character_id=character_id, primary_candidate_id=None,
                parent_turn_id=assistant_turn_id, created_at=now,
            ),
            Turn(
                id=draft_turn_id, chat_id=chat_id, turn_no=4, author_type="CHARACTER",
                state="OK", author_character_id=character_id, primary_candidate_id=None,
                parent_turn_id=error_cand_turn_id, created_at=now,
            ),
            Turn(
                id=greeting_turn_id, chat_id=chat_id, turn_no=5, author_type="CHARACTER",
                state="OK", author_character_id=character_id, is_proactive=True,
                primary_candidate_id=None, parent_turn_id=draft_turn_id, created_at=now,
            ),
        ]
        session.add_all(turns)
        await session.commit()

        candidates = [
            Candidate(
                id=user_cand_id, turn_id=user_turn_id, candidate_no=1,
                content="user question text", is_final=True, created_at=now, extra={},
            ),
            Candidate(
                id=good_cand_id, turn_id=assistant_turn_id, candidate_no=1,
                content="Stored production reply.", is_final=True,
                model_type="deepseek:deepseek-v4-flash", created_at=now, extra={},
            ),
            Candidate(
                id=error_cand_id, turn_id=error_cand_turn_id, candidate_no=1,
                content="partial", is_final=True, created_at=now,
                extra={"error_code": "llm_service_error"},
            ),
            Candidate(
                id=draft_cand_id, turn_id=draft_turn_id, candidate_no=1,
                content="draft", is_final=False, created_at=now, extra={},
            ),
            Candidate(
                id=greeting_cand_id, turn_id=greeting_turn_id, candidate_no=1,
                content="hi there", is_final=True, created_at=now,
                extra={"source": "greeting"},
            ),
        ]
        session.add_all(candidates)
        await session.commit()

        # back-fill primary candidate pointers (circular FK)
        user_turn = await session.get(Turn, user_turn_id)
        user_turn.primary_candidate_id = user_cand_id
        assistant_turn = await session.get(Turn, assistant_turn_id)
        assistant_turn.primary_candidate_id = good_cand_id
        await session.commit()

    return {
        "good": good_cand_id,
        "error": error_cand_id,
        "draft": draft_cand_id,
        "greeting": greeting_cand_id,
        "missing": uuid.uuid4(),
    }


def _case(source_candidate_id) -> GoldenCase:
    return GoldenCase.model_validate(
        make_roleplay_case("cand-case", source_candidate_id=str(source_candidate_id))
        | {"character_snapshot": {"name": "Cand Char", "description": "d", "system_prompt": "stay in character"}}
    )


def _context(factory) -> CaseExecutionContext:
    return CaseExecutionContext(
        run_id=uuid.uuid4(),
        content_mode="none",
        judge=StubJudge(),
        generation_service=None,
        generation_route=None,
    )


async def test_candidate_replay_scores_stored_content(candidate_env) -> None:
    runner = CandidateReplayRunner(session_factory=candidate_env["factory"])
    outcome = await runner.run_case(_case(candidate_env["good"]), _context(candidate_env["factory"]))

    assert outcome.status == "completed"
    assert outcome.response_status == "valid"
    assert set(outcome.dimension_scores) == {
        "persona_fidelity",
        "multi_turn_consistency",
        "coherence_progression",
        "empathy_appropriateness",
        "engagement_diversity",
    }
    assert outcome.generation_model_type == "deepseek:deepseek-v4-flash"
    assert outcome.output_ref == str(candidate_env["good"])
    assert outcome.rule_metrics["ooc"]["predicted"] is False


