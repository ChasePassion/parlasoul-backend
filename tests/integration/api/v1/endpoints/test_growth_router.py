from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers import growth
from src.core.dependencies import get_growth_service
from src.db.models import User


class _StubGrowthService:
    def __init__(self) -> None:
        self.get_calendar = AsyncMock()
        self.apply_makeup = AsyncMock()
        self.get_overview = AsyncMock()
        self.list_pending_share_cards = AsyncMock()


def _create_app(service: _StubGrowthService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(growth.router)

    async def _override_current_user() -> User:
        return User(id=uuid.uuid4(), email="user@example.com")

    app.dependency_overrides[deps.get_current_user] = _override_current_user
    app.dependency_overrides[get_growth_service] = lambda: service
    return app




def test_growth_routes_return_400_for_invalid_params() -> None:
    service = _StubGrowthService()
    app = _create_app(service)

    with TestClient(app) as client:
        assert client.get("/v1/growth/calendar?month=202604").status_code == 400
        assert client.post("/v1/growth/make-up", json={}).status_code == 422
        assert client.get("/v1/growth/overview?focus_character_id=bad-uuid").status_code == 400
        assert client.get("/v1/growth/share-cards/pending?chat_id=bad-uuid").status_code == 400

    service.get_calendar.assert_not_awaited()
    service.apply_makeup.assert_not_awaited()
    service.get_overview.assert_not_awaited()
    service.list_pending_share_cards.assert_not_awaited()
