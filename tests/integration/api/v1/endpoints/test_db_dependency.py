from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.session import get_db


def _create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/db-ping")
    async def db_ping(db: AsyncSession = Depends(get_db)) -> dict[str, int]:
        result = await db.exec(text("SELECT 1"))
        return {"value": int(result.scalar_one())}

    return app


def test_get_db_override_with_sqlite_in_memory() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app = _create_app()
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        resp = client.get("/db-ping")

    assert resp.status_code == 200
    assert resp.json()["value"] == 1

    asyncio.run(engine.dispose())
