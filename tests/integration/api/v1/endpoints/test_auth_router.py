from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import auth
from src.db.models import User


def _create_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth.router)

    async def _override_current_user() -> User:
        return User(
            id=uuid.uuid4(),
            email="viewer@example.com",
            display_name="Viewer",
            username="viewer",
            email_verified=True,
            avatar_url="https://example.com/avatar.png",
            created_at=datetime.now(UTC),
            last_login_at=datetime.now(UTC),
        )

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    return app


def test_read_users_me_returns_profile() -> None:
    app = _create_app()

    with TestClient(app) as client:
        resp = client.get("/v1/auth/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["email"] == "viewer@example.com"
    assert body["data"]["username"] == "viewer"
    assert body["data"]["email_verified"] is True
    assert "avatar_url" not in body["data"]
