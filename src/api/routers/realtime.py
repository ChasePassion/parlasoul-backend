"""Realtime WebRTC session APIs."""

from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import APIRouter, Depends

from src.api import deps
from src.api.response import success_response
from src.core.dependencies import (
    get_realtime_ice_config_service,
    get_realtime_session_manager,
    get_realtime_telemetry_service,
)
from src.core.exceptions import APIError
from src.db.models import User
from src.realtime.schemas.server_events import (
    RealtimeIceConfigResponse,
    RealtimeSessionCreateRequest,
    RealtimeSessionCreateResponse,
)
from src.realtime.telemetry import RealtimeTelemetryRecorderFactory
from src.repositories.character_repository import CharacterRepository
from src.repositories.chat_repository import ChatRepository
from src.schemas.common import SuccessEnvelope
from src.services.realtime_telemetry_service import RealtimeTelemetryService

router = APIRouter(prefix="/v1/realtime", tags=["realtime"])


@router.get("/config", response_model=SuccessEnvelope[RealtimeIceConfigResponse])
async def get_realtime_config(
    current_user: User = Depends(deps.get_current_user),
    ice_config_service=Depends(get_realtime_ice_config_service),
):
    return success_response(
        RealtimeIceConfigResponse(
            ice_servers=ice_config_service.build_client_ice_servers(user_id=current_user.id),
            credential_ttl_seconds=ice_config_service.credential_ttl_seconds(),
        ),
        status_code=200,
    )


@router.post("/session", response_model=SuccessEnvelope[RealtimeSessionCreateResponse])
async def create_realtime_session(
    request: RealtimeSessionCreateRequest,
    current_user: User = Depends(deps.get_current_user),
    chat_repo: ChatRepository = Depends(deps.get_chat_repository),
    character_repo: CharacterRepository = Depends(deps.get_character_repository),
    session_manager=Depends(get_realtime_session_manager),
    ice_config_service=Depends(get_realtime_ice_config_service),
    telemetry_service: RealtimeTelemetryService = Depends(get_realtime_telemetry_service),
):
    try:
        chat_id = uuid.UUID(request.chat_id)
        character_id = uuid.UUID(request.character_id)
    except ValueError as exc:
        raise APIError(422, "validation_failed", "validation failed: chat_id/character_id invalid") from exc

    chat = await chat_repo.get_by_id(chat_id)
    character = await character_repo.get_by_id(character_id)
    if chat is None or character is None:
        raise APIError(404, "resource_not_found", "resource not found: chat or character missing")

    metrics_recorder = None
    session_id = None
    if request.observation_id is not None:
        session_id = session_manager.generate_session_id()
        await telemetry_service.attach_runtime_session(
            observation_id=request.observation_id,
            user_id=current_user.id,
            chat_id=chat_id,
            character_id=character_id,
            rtc_session_id=session_id,
        )
        metrics_recorder = RealtimeTelemetryRecorderFactory().create(
            observation_id=request.observation_id,
            user_id=current_user.id,
            runtime_session_id=session_id,
        )

    negotiation_started = time.monotonic()
    try:
        session, answer = await session_manager.create_session(
            current_user=current_user,
            chat=chat,
            character=character,
            offer=request.sdp,
            session_id=session_id,
            metrics_sink=metrics_recorder,
        )
    except BaseException as exc:
        if metrics_recorder is not None:
            duration_ms = max(0, int((time.monotonic() - negotiation_started) * 1_000))
            if isinstance(exc, asyncio.CancelledError):
                await metrics_recorder.finalize(
                    session_id=session_id,
                    status="cancelled",
                    duration_ms=duration_ms,
                )
            else:
                error_code = (
                    _NEGOTIATION_ERROR_CODES.get(exc.code, "negotiation_failed")
                    if isinstance(exc, APIError)
                    else "negotiation_failed"
                )
                await metrics_recorder.finalize_failed(
                    failure_stage="signaling",
                    error_code=error_code,
                    duration_ms=duration_ms,
                )
            await metrics_recorder.close()
        raise
    return success_response(
        RealtimeSessionCreateResponse(
            session_id=session.session_id,
            chat_id=request.chat_id,
            character_id=request.character_id,
            sdp=answer,
            ice_servers=ice_config_service.build_client_ice_servers(user_id=current_user.id),
            credential_ttl_seconds=ice_config_service.credential_ttl_seconds(),
        ),
        status_code=201,
    )


@router.delete("/session/{session_id}")
async def delete_realtime_session(
    session_id: str,
    current_user: User = Depends(deps.get_current_user),
    session_manager=Depends(get_realtime_session_manager),
):
    await session_manager.close_session(session_id, user_id=current_user.id)
    return success_response(status_code=200)


_NEGOTIATION_ERROR_CODES = {
    "external_timeout": "negotiation_timeout",
    "forbidden": "forbidden",
    "invalid_param": "invalid_request",
    "resource_conflict": "session_conflict",
    "validation_failed": "invalid_request",
}
