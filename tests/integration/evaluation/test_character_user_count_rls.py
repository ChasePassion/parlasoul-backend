"""Integration test: public character user counts must survive RLS.

Character cards expose a public distinct-user count. The ``chats``/``turns``
RLS policies scope rows to the request user, so a public card request has no
user context and the aggregate would see zero rows. The repository switches to
the system context only for that aggregate and restores the caller context.

This test runs the real policies under a non-superuser role so the RLS
enforcement path is exercised rather than bypassed by the database owner.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import MetaData, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.config import settings
from src.db.models import AuthorType, Character, Chat, Turn, User
from src.repositories.character_repository import CharacterRepository

_TABLES = [
    User.__table__,
    Character.__table__,
    Chat.__table__,
    Turn.__table__,
]

_SAFE_USER_ID = "NULLIF(NULLIF(current_setting('app.current_user_id', true), ''), 'system')::uuid"
_IS_SYSTEM = "current_setting('app.current_user_id', true) = 'system'"


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _enable_rls(conn, quoted_schema: str) -> None:
    chats = f"{quoted_schema}.chats"
    turns = f"{quoted_schema}.turns"
    await conn.execute(text(f"ALTER TABLE {chats} ENABLE ROW LEVEL SECURITY"))
    await conn.execute(text(f"ALTER TABLE {chats} FORCE ROW LEVEL SECURITY"))
    await conn.execute(text(f"ALTER TABLE {turns} ENABLE ROW LEVEL SECURITY"))
    await conn.execute(text(f"ALTER TABLE {turns} FORCE ROW LEVEL SECURITY"))
    await conn.execute(
        text(
            f"""
            CREATE POLICY chats_scope ON {chats}
              FOR ALL
              USING ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
              WITH CHECK ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
            """
        )
    )
    await conn.execute(
        text(
            f"""
            CREATE POLICY turns_scope ON {turns}
              FOR ALL
              USING (
                {_IS_SYSTEM}
                OR EXISTS (
                  SELECT 1 FROM {chats} c
                  WHERE c.id = {turns}.chat_id
                    AND c.user_id = {_SAFE_USER_ID}
                )
              )
              WITH CHECK (
                {_IS_SYSTEM}
                OR EXISTS (
                  SELECT 1 FROM {chats} c
                  WHERE c.id = {turns}.chat_id
                    AND c.user_id = {_SAFE_USER_ID}
                )
              )
            """
        )
    )


@pytest.fixture()
async def count_pg_env(ensure_pg_enum_types) -> AsyncIterator[dict[str, Any]]:
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        pytest.skip("PostgreSQL integration test requires postgresql+asyncpg DATABASE_URL.")

    schema_name = f"test_character_count_{uuid.uuid4().hex}"
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
    isolated_meta = MetaData()
    isolated_tables = [table.tometadata(isolated_meta, schema=schema_name) for table in _TABLES]

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))

        async with test_engine.begin() as conn:
            await ensure_pg_enum_types(conn)
            await conn.run_sync(isolated_meta.create_all, tables=isolated_tables)
            await _enable_rls(conn, quoted_schema)

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
async def restricted_count_role(count_pg_env) -> AsyncIterator[str]:
    role_name = f"character_count_probe_{uuid.uuid4().hex[:12]}"
    quoted_role = _quote_ident(role_name)
    quoted_schema = count_pg_env["quoted_schema"]
    admin_engine = count_pg_env["admin_engine"]

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {quoted_role} NOLOGIN"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}"))
            for table in ("chats", "turns"):
                await conn.execute(
                    text(f"GRANT SELECT ON {quoted_schema}.{table} TO {quoted_role}")
                )
    except DBAPIError as exc:
        pytest.skip(f"cannot create probe role in this environment: {exc.__class__.__name__}")

    yield role_name

    async with admin_engine.begin() as conn:
        await conn.execute(text(f"DROP OWNED BY {quoted_role}"))
        await conn.execute(text(f"DROP ROLE IF EXISTS {quoted_role}"))


@pytest.mark.asyncio
async def test_character_user_count_reads_across_rls_scope(
    count_pg_env,
    restricted_count_role,
) -> None:
    session_factory = count_pg_env["session_factory"]
    user_a = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        username=f"user_{uuid.uuid4().hex[:8]}",
    )
    user_b = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        username=f"user_{uuid.uuid4().hex[:8]}",
    )
    character = Character(
        id=uuid.uuid4(),
        name="Count Probe",
        description="probe",
        system_prompt="probe",
        creator_id=user_a.id,
    )
    chat_a = Chat(id=uuid.uuid4(), user_id=user_a.id, character_id=character.id)
    chat_b = Chat(id=uuid.uuid4(), user_id=user_b.id, character_id=character.id)
    turn_a = Turn(
        id=uuid.uuid4(),
        chat_id=chat_a.id,
        turn_no=1,
        author_type=AuthorType.USER.value,
        author_user_id=user_a.id,
    )
    turn_b = Turn(
        id=uuid.uuid4(),
        chat_id=chat_b.id,
        turn_no=1,
        author_type=AuthorType.USER.value,
        author_user_id=user_b.id,
    )
    character_turn = Turn(
        id=uuid.uuid4(),
        chat_id=chat_a.id,
        turn_no=2,
        author_type=AuthorType.CHARACTER.value,
        author_character_id=character.id,
    )

    async with session_factory() as session:
        session.info["current_user_id"] = "system"
        session.add_all([user_a, user_b, character, chat_a, chat_b, turn_a, turn_b, character_turn])
        await session.commit()

    quoted_role = _quote_ident(restricted_count_role)
    async with session_factory() as session:
        await session.execute(text(f"SET ROLE {quoted_role}"))
        session.info["current_user_id"] = ""

        under_rls = (
            await session.execute(
                select(func.count(func.distinct(Turn.author_user_id)))
                .join(Chat, Chat.id == Turn.chat_id)
                .where(
                    Chat.character_id == character.id,
                    Turn.author_type == AuthorType.USER.value,
                    Turn.author_user_id.isnot(None),
                )
            )
        ).scalar_one()
        assert int(under_rls or 0) == 0

        repo = CharacterRepository(session)
        assert await repo.count_distinct_users_by_character(character.id) == 2
        assert await repo.batch_count_distinct_users([character.id]) == {character.id: 2}
        restored = (
            await session.execute(text("SELECT current_setting('app.current_user_id', true)"))
        ).scalar_one_or_none()
        assert restored == ""
