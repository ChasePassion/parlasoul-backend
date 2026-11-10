from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deps
from src.api.exceptions import register_exception_handlers
from src.api.routers.realtime_telemetry import router
from src.core.dependencies import get_realtime_telemetry_service


def _build_client(service: SimpleNamespace, user_id: uuid.UUID) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(id=user_id)
    app.dependency_overrides[get_realtime_telemetry_service] = lambda: service
    return TestClient(app)




def test_event_route_rejects_private_payload_before_service() -> None:
    service = SimpleNamespace(ingest_client_events=AsyncMock())
    with _build_client(service, uuid.uuid4()) as client:
        response = client.post(
            f"/v1/realtime/telemetry/sessions/{uuid.uuid4()}/events/batch",
            json={
                "events": [
                    {
                        "seq": 0,
                        "event_type": "protocol.error",
                        "occurred_offset_ms": 1,
                        "payload": {"transcript": "must-not-persist"},
                    }
                ]
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    service.ingest_client_events.assert_not_awaited()


