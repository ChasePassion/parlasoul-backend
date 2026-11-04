"""Persistent realtime call telemetry APIs."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query

from src.api import deps
from src.api.response import success_response
from src.core.dependencies import get_realtime_telemetry_service
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.services.realtime_telemetry_service import RealtimeTelemetryService
from src.schemas.realtime_telemetry import (
    RealtimeTelemetryBatchResponse,
    RealtimeTelemetryEventsBatchRequest,
    RealtimeTelemetryMode,
    RealtimeTelemetryScenario,
    RealtimeTelemetrySessionCreateRequest,
    RealtimeTelemetrySessionCreateResponse,
    RealtimeTelemetrySessionDetailResponse,
    RealtimeTelemetrySessionPageResponse,
    RealtimeTelemetrySessionResponse,
    RealtimeTelemetrySessionUpdateRequest,
    RealtimeTelemetryStatus,
    RealtimeWebRTCStatsBatchRequest,
)

router = APIRouter(prefix="/v1/realtime/telemetry", tags=["realtime-telemetry"])

__all__ = ["get_realtime_telemetry_service", "router"]


@router.post(
    "/sessions",
    response_model=SuccessEnvelope[RealtimeTelemetrySessionCreateResponse],
)
async def create_telemetry_session(
    request: RealtimeTelemetrySessionCreateRequest,
    current_user: User = Depends(deps.get_current_user),
    service: RealtimeTelemetryService = Depends(get_realtime_telemetry_service),
):
    data = await service.create_session(current_user=current_user, request=request)
    return success_response(data, status_code=201)


@router.post(
    "/sessions/{observation_id}/events/batch",
    response_model=SuccessEnvelope[RealtimeTelemetryBatchResponse],
)
async def ingest_telemetry_events(
    observation_id: uuid.UUID,
    request: RealtimeTelemetryEventsBatchRequest,
    current_user: User = Depends(deps.get_current_user),
    service: RealtimeTelemetryService = Depends(get_realtime_telemetry_service),
):
    data = await service.ingest_client_events(
        current_user=current_user,
        observation_id=observation_id,
        request=request,
    )
    return success_response(data, status_code=200)


@router.post(
    "/sessions/{observation_id}/webrtc-stats/batch",
    response_model=SuccessEnvelope[RealtimeTelemetryBatchResponse],
)
async def ingest_webrtc_stats(
    observation_id: uuid.UUID,
    request: RealtimeWebRTCStatsBatchRequest,
    current_user: User = Depends(deps.get_current_user),
    service: RealtimeTelemetryService = Depends(get_realtime_telemetry_service),
):
    data = await service.ingest_stats_samples(
        current_user=current_user,
        observation_id=observation_id,
        request=request,
    )
    return success_response(data, status_code=200)


@router.patch(
    "/sessions/{observation_id}",
    response_model=SuccessEnvelope[RealtimeTelemetrySessionResponse],
)
async def update_telemetry_session(
    observation_id: uuid.UUID,
    request: RealtimeTelemetrySessionUpdateRequest,
    current_user: User = Depends(deps.get_current_user),
    service: RealtimeTelemetryService = Depends(get_realtime_telemetry_service),
):
    data = await service.update_session(
        current_user=current_user,
        observation_id=observation_id,
        request=request,
    )
    return success_response(data, status_code=200)


@router.get(
    "/sessions",
    response_model=SuccessEnvelope[RealtimeTelemetrySessionPageResponse],
)
async def list_telemetry_sessions(
    mode: RealtimeTelemetryMode | None = Query(None),
    status: RealtimeTelemetryStatus | None = Query(None),
    scenario: RealtimeTelemetryScenario | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(deps.get_current_user),
    service: RealtimeTelemetryService = Depends(get_realtime_telemetry_service),
):
    data = await service.list_sessions(
        current_user=current_user,
        mode=mode,
        status=status,
        scenario=scenario,
        cursor=cursor,
        limit=limit,
    )
    return success_response(data, status_code=200)


@router.get(
    "/sessions/{observation_id}",
    response_model=SuccessEnvelope[RealtimeTelemetrySessionDetailResponse],
)
async def get_telemetry_session_detail(
    observation_id: uuid.UUID,
    events_limit: int = Query(500, ge=1, le=2_000),
    turns_limit: int = Query(200, ge=1, le=500),
    stats_limit: int = Query(3_600, ge=1, le=10_000),
    current_user: User = Depends(deps.get_current_user),
    service: RealtimeTelemetryService = Depends(get_realtime_telemetry_service),
):
    data = await service.get_session_detail(
        current_user=current_user,
        observation_id=observation_id,
        events_limit=events_limit,
        turns_limit=turns_limit,
        stats_limit=stats_limit,
    )
    return success_response(data, status_code=200)


__all__ = ["get_realtime_telemetry_service", "router"]
