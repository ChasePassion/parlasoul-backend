from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.exceptions import register_exception_handlers
from src.api.routers import discover
from src.core.dependencies import get_discover_config_service
from src.schemas.discover import DiscoverConfigResponse, DiscoverHeroItem


class _StubDiscoverConfigService:
    """Tests inject professional_character_ids; never hardcode operational UUIDs
    here, since ops reclassifies characters on /srv/parlasoul/config/discover.json
    far more often than this code changes.
    """

    def __init__(
        self,
        *,
        professional_character_ids: list[str] | None = None,
    ) -> None:
        self._professional_character_ids = professional_character_ids

    def get_public_config(self) -> DiscoverConfigResponse:
        return DiscoverConfigResponse(
            hero_character_ids=[
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
            hero_items=[
                DiscoverHeroItem(
                    character_id="11111111-1111-1111-1111-111111111111",
                    image_key="images/discover/heroes/elon-21-9.jpg",
                    image_url="/media/images/discover/heroes/elon-21-9.jpg",
                    cta_text="开始对话",
                )
            ],
            professional_character_ids=self._professional_character_ids or [],
        )


def _create_app(service: _StubDiscoverConfigService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(discover.router)
    app.dependency_overrides[get_discover_config_service] = lambda: service
    return app


def test_discover_config_route_returns_runtime_config() -> None:
    app = _create_app(_StubDiscoverConfigService())

    with TestClient(app) as client:
        resp = client.get("/v1/discover/config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "ok"
    assert body["data"]["hero_character_ids"] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert body["data"]["hero_items"] == [
        {
            "character_id": "11111111-1111-1111-1111-111111111111",
            "image_key": "images/discover/heroes/elon-21-9.jpg",
            "image_url": "/media/images/discover/heroes/elon-21-9.jpg",
            "cta_text": "开始对话",
        }
    ]


def test_discover_config_route_exposes_professional_character_ids_field() -> None:
    """Schema contract: the response always includes professional_character_ids,
    regardless of operational values."""
    app = _create_app(_StubDiscoverConfigService())

    with TestClient(app) as client:
        resp = client.get("/v1/discover/config")

    assert resp.status_code == 200
    assert "professional_character_ids" in resp.json()["data"]
    assert isinstance(resp.json()["data"]["professional_character_ids"], list)


def test_discover_config_route_passes_professional_ids_through_unchanged() -> None:
    """Pass-through contract: whatever the service returns is delivered verbatim
    to the client. We use freshly generated UUIDs so the test does not depend
    on operational character IDs."""
    generated_ids = [str(uuid.uuid4()) for _ in range(3)]
    stub = _StubDiscoverConfigService(professional_character_ids=generated_ids)
    app = _create_app(stub)

    with TestClient(app) as client:
        resp = client.get("/v1/discover/config")

    assert resp.status_code == 200
    assert resp.json()["data"]["professional_character_ids"] == generated_ids


def test_discover_config_route_defaults_professional_ids_to_empty_list() -> None:
    """Default contract: when the service returns no professional ids, the
    field is serialized as an empty list (not null, not missing)."""
    app = _create_app(_StubDiscoverConfigService())

    with TestClient(app) as client:
        resp = client.get("/v1/discover/config")

    assert resp.status_code == 200
    assert resp.json()["data"]["professional_character_ids"] == []
