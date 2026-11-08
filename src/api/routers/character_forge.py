"""Conversational character creation (Character Forge) API."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response, StreamingResponse

from src.api import deps
from src.api.response import SSE_HEADERS, success_response
from src.core.dependencies import get_character_forge_service
from src.db.models import User
from src.schemas.character_forge import (
    ForgeSessionDetail,
    ForgeSessionList,
    ForgeSessionRenameRequest,
    ForgeSessionSummary,
    ForgeSteerRequest,
    ForgeSteerResult,
    ForgeStreamRequest,
)
from src.schemas.common import SuccessEnvelope
from src.services.character_forge_service import CharacterForgeService

router = APIRouter(prefix="/v1/character-forge", tags=["character-forge"])

# Next.js rewrite proxy aborts a socket after 30s of idle time. Emit SSE
# comment frames during long LLM turns so the connection stays alive.
_SSE_HEARTBEAT_SECONDS = 10.0
_SSE_HEARTBEAT_FRAME = ": ping\n\n"


async def _with_sse_heartbeat(
    source: AsyncIterator[str],
    *,
    interval: float = _SSE_HEARTBEAT_SECONDS,
) -> AsyncGenerator[str, None]:
    """Forward SSE frames while emitting comment frames during idle gaps.

    One dedicated task drains the source so every ``__anext__`` runs in the
    same contextvars Context. Splitting the iteration across tasks would break
    the log-context tokens used by the LLM gateway.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    errors: list[Exception] = []

    async def pump() -> None:
        try:
            async for chunk in source:
                await queue.put(chunk)
        except Exception as exc:
            errors.append(exc)
        finally:
            await queue.put(None)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield _SSE_HEARTBEAT_FRAME
                continue
            if chunk is None:
                if errors:
                    raise errors[0]
                return
            yield chunk
    finally:
        if not pump_task.done():
            pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        aclose = getattr(source, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except RuntimeError:
                # Generator is already closing during task cancellation.
                pass


@router.get("/sessions", response_model=SuccessEnvelope[ForgeSessionList])
async def list_sessions(
    limit: int = Query(20, ge=1, le=50),
    cursor: datetime | None = Query(None),
    current_user: User = Depends(deps.get_current_user),
    service: CharacterForgeService = Depends(get_character_forge_service),
) -> SuccessEnvelope[ForgeSessionList]:
    data = await service.list_sessions(current_user, limit=limit, cursor=cursor)
    return success_response(data, status_code=200)


@router.post(
    "/sessions",
    response_model=SuccessEnvelope[ForgeSessionDetail],
    status_code=201,
)
async def create_session(
    current_user: User = Depends(deps.get_current_user),
    service: CharacterForgeService = Depends(get_character_forge_service),
) -> SuccessEnvelope[ForgeSessionDetail]:
    data = await service.create_session(current_user)
    return success_response(data, status_code=201)


@router.get(
    "/sessions/{session_id}",
    response_model=SuccessEnvelope[ForgeSessionDetail],
)
async def get_session(
    session_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: CharacterForgeService = Depends(get_character_forge_service),
) -> SuccessEnvelope[ForgeSessionDetail]:
    data = await service.get_session_detail(current_user, session_id)
    return success_response(data, status_code=200)


@router.patch(
    "/sessions/{session_id}",
    response_model=SuccessEnvelope[ForgeSessionSummary],
)
async def rename_session(
    session_id: uuid.UUID,
    request: ForgeSessionRenameRequest,
    current_user: User = Depends(deps.get_current_user),
    service: CharacterForgeService = Depends(get_character_forge_service),
) -> SuccessEnvelope[ForgeSessionSummary]:
    data = await service.rename_session(current_user, session_id, request.title)
    return success_response(data, status_code=200)


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: CharacterForgeService = Depends(get_character_forge_service),
):
    await service.delete_session(current_user, session_id)
    return success_response({"deleted": True}, status_code=200)


@router.post("/sessions/{session_id}/stream")
async def stream_reply(
    session_id: uuid.UUID,
    request: ForgeStreamRequest,
    current_user: User = Depends(deps.get_current_user),
    service: CharacterForgeService = Depends(get_character_forge_service),
) -> StreamingResponse:
    # Validate before the response starts so 404/409 can still be JSON.
    await service.assert_stream_available(current_user, session_id)

    return StreamingResponse(
        _with_sse_heartbeat(
            service.stream_reply(
                current_user, session_id, request.content, request.images
            )
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/sessions/{session_id}/steer",
    response_model=SuccessEnvelope[ForgeSteerResult],
)
async def steer_reply(
    session_id: uuid.UUID,
    request: ForgeSteerRequest,
    current_user: User = Depends(deps.get_current_user),
    service: CharacterForgeService = Depends(get_character_forge_service),
) -> SuccessEnvelope[ForgeSteerResult]:
    data = await service.steer(
        current_user,
        session_id,
        content=request.content,
        steer_id=request.steer_id,
    )
    return success_response(data, status_code=200)


@router.get("/voice-previews/{session_id}/{preview_id}/audio")
async def stream_voice_preview(
    session_id: uuid.UUID,
    preview_id: str,
    format: str = Query("mp3", pattern="^(mp3|pcm|opus)$"),
    current_user: User = Depends(deps.get_current_user),
    service: CharacterForgeService = Depends(get_character_forge_service),
) -> Response:
    mime_type, audio_stream = await service.render_preview_audio(
        current_user, session_id, preview_id, format
    )

    async def body():
        async for chunk in audio_stream:
            yield chunk

    return StreamingResponse(body(), media_type=mime_type)
