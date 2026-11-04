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
from src.schemas.realtime_telemetry import (
    RealtimeTelemetryBatchResponse,
    RealtimeTelemetrySessionCreateResponse,
    RealtimeTelemetrySessionPageResponse,
)


def _build_client(service: SimpleNamespace, user_id: uuid.UUID) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(id=user_id)
    app.dependency_overrides[get_realtime_telemetry_service] = lambda: service
    return TestClient(app)


def test_create_and_list_telemetry_routes_use_success_envelope() -> None:
    observation_id = uuid.uuid4()
    service = SimpleNamespace(
        create_session=AsyncMock(
            return_value=RealtimeTelemetrySessionCreateResponse(
                observation_id=observation_id
            )
        ),
        list_sessions=AsyncMock(
            return_value=RealtimeTelemetrySessionPageResponse(items=[])
        ),
    )
    chat_id = uuid.uuid4()
    character_id = uuid.uuid4()
    with _build_client(service, uuid.uuid4()) as client:
        response = client.post(
            "/v1/realtime/telemetry/sessions",
            json={
                "mode": "chat",
                "chat_id": str(chat_id),
                "character_id": str(character_id),
                "scenario": "normal",
            },
        )
        listed = client.get("/v1/realtime/telemetry/sessions")

    assert response.status_code == 201
    assert response.json() == {
        "code": "ok",
        "message": "ok",
        "status": 201,
        "data": {
            "observation_id": str(observation_id),
            "schema_version": 1,
            "stats_interval_ms": 1000,
            "max_batch_size": 50,
        },
    }
    assert listed.status_code == 200
    assert listed.json()["data"] == {"items": [], "next_cursor": None}


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


def test_event_batch_route_returns_idempotency_counts() -> None:
    observation_id = uuid.uuid4()
    service = SimpleNamespace(
        ingest_client_events=AsyncMock(
            return_value=RealtimeTelemetryBatchResponse(
                accepted_count=0,
                duplicate_count=1,
            )
        )
    )
    with _build_client(service, uuid.uuid4()) as client:
        response = client.post(
            f"/v1/realtime/telemetry/sessions/{observation_id}/events/batch",
            json={
                "events": [
                    {
                        "seq": 7,
                        "event_type": "peer_connection.state",
                        "occurred_offset_ms": 100,
                        "payload": {"state": "connected"},
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["data"] == {"accepted_count": 0, "duplicate_count": 1}
