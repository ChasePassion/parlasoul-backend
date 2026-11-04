from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.config import settings
from src.core.uow import UnitOfWork
from src.db.models import User, UserSettings
from src.repositories.user_settings_repository import UserSettingsRepository
from src.schemas.user_settings import UserSettingsUpdate
from src.services.user_settings_service import UserSettingsService


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@pytest.fixture()
async def postgres_session() -> AsyncIterator[AsyncSession]:
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        pytest.skip("PostgreSQL integration test requires postgresql+asyncpg DATABASE_URL.")

    schema_name = f"test_user_settings_{uuid.uuid4().hex}"
    quoted_schema = _quote_ident(schema_name)
    test_connect_args = dict(settings.database_connect_args)
    server_settings = dict(test_connect_args.get("server_settings", {}))
    server_settings["search_path"] = f"{schema_name},public"
    test_connect_args["server_settings"] = server_settings
    admin_engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        connect_args=settings.database_connect_args,
    )
    test_engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        connect_args=test_connect_args,
    )

    try:
        try:
            async with admin_engine.begin() as conn:
                await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        except Exception as exc:
            await admin_engine.dispose()
            await test_engine.dispose()
            pytest.skip(f"PostgreSQL test schema could not be created: {exc}")

        async with test_engine.begin() as conn:
            await conn.execute(text(f"SET search_path TO {quoted_schema}, public"))
            await conn.run_sync(
                SQLModel.metadata.create_all,
                tables=[User.__table__, UserSettings.__table__],
            )

        session_factory = async_sessionmaker(
            bind=test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        async with session_factory() as session:
            session.info["current_user_id"] = ""
            session.info["test_search_path"] = f"{quoted_schema}, public"
            await session.exec(text(f"SET search_path TO {quoted_schema}, public"))
            yield session
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        await test_engine.dispose()
        await admin_engine.dispose()


def _build_user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"settings-{uuid.uuid4().hex}@example.com",
        username=f"user_{uuid.uuid4().hex[:16]}",
    )


class _SubscriptionServiceStub:
    def __init__(self) -> None:
        self.assert_feature = AsyncMock()


@pytest.mark.asyncio
async def test_user_settings_service_persists_defaults_and_updates_in_postgres(
    postgres_session: AsyncSession,
) -> None:
    user = _build_user()
    postgres_session.add(user)
    await postgres_session.commit()

    subscription_service = _SubscriptionServiceStub()
    repo = UserSettingsRepository(postgres_session)
    service = UserSettingsService(repo, UnitOfWork(postgres_session), subscription_service)

    created = await service.get_my_settings(current_user=user)
    updated = await service.update_my_settings(
        UserSettingsUpdate(
            display_mode="detailed",
            message_font_size=18,
            memory_enabled=False,
        ),
        current_user=user,
    )
    persisted = (
        await postgres_session.exec(
            select(UserSettings).where(UserSettings.user_id == user.id)
        )
    ).one()

    assert created.display_mode == "concise"
    assert created.memory_enabled is True
    assert updated.display_mode == "detailed"
    assert updated.message_font_size == 18
    assert updated.memory_enabled is False
    assert persisted.display_mode == "detailed"
    assert persisted.message_font_size == 18
    assert persisted.memory_enabled is False
    subscription_service.assert_feature.assert_awaited_once_with(user, "memory_feature")


@pytest.mark.asyncio
async def test_user_settings_repository_requires_existing_user_in_postgres(
    postgres_session: AsyncSession,
) -> None:
    repo = UserSettingsRepository(postgres_session)
    missing_user_id = uuid.uuid4()

    with pytest.raises(IntegrityError):
        async with UnitOfWork(postgres_session).transactional():
            await repo.create_default(
                missing_user_id,
                default_font_size=16,
                default_display_mode="concise",
                default_memory_enabled=True,
                default_reply_card_enabled=True,
                default_mixed_input_auto_translate_enabled=True,
                default_auto_read_aloud_enabled=True,
                default_preferred_expression_bias_enabled=True,
                default_proactive_enabled=False,
                default_timezone="UTC",
            )

    await postgres_session.rollback()
    await postgres_session.exec(
        text(f"SET search_path TO {postgres_session.info['test_search_path']}")
    )
    rows = (
        await postgres_session.exec(
            select(UserSettings).where(UserSettings.user_id == missing_user_id)
        )
    ).all()

    assert rows == []
