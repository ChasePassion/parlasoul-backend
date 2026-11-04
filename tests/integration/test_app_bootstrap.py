from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.bootstrap import app_factory


def test_create_app_exposes_health() -> None:
    @asynccontextmanager
    async def _fake_engine_connect():
        conn = SimpleNamespace(
            execute=AsyncMock(),
            dialect=SimpleNamespace(name="sqlite"),
        )
        yield conn

    app_factory.engine = SimpleNamespace(  # type: ignore[assignment]
        connect=_fake_engine_connect,
        dispose=AsyncMock(),
    )
    app_factory.get_memory_system = lambda: object()  # type: ignore[assignment]

    app = app_factory.create_app()
    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "ok"
    assert body["data"]["status"] == "ok"
