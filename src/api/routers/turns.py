"""Turn-level APIs for candidate switching, regen, and user edit branching."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse

from src.api import deps
from src.api.response import SSE_HEADERS, success_response
from src.core.dependencies import get_chat_service, get_turn_service
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.learning import FeedbackCardPayload
from src.schemas.turns import (
    TurnSelectRequest,
    TurnSelectResponse,
    TurnSelectSnapshotResponse,
    UserTurnEditStreamRequest,
)
from src.services.chat_service import ChatService
from src.services.turn_service import TurnService

router = APIRouter(prefix="/v1/turns", tags=["turns"])


@router.post("/{turn_id}/select", response_model=SuccessEnvelope[TurnSelectResponse])
async def select_turn_candidate(
    turn_id: uuid.UUID,
    req: TurnSelectRequest,
    current_user: User = Depends(deps.get_current_user),
    service: TurnService = Depends(get_turn_service),
) -> SuccessEnvelope[TurnSelectResponse]:
    data = await service.select_candidate(
        current_user=current_user,
        turn_id=turn_id,
        candidate_no=req.candidate_no,
    )
    return success_response(data, status_code=200)


@router.post("/{turn_id}/select/snapshot", response_model=SuccessEnvelope[TurnSelectSnapshotResponse])
async def select_turn_candidate_with_snapshot(
    turn_id: uuid.UUID,
    req: TurnSelectRequest,
    limit: int = Query(50, ge=1, le=100),
    include_learning_data: bool = Query(
        True,
        description="Include sentence card and input transform metadata in candidate.extra",
    ),
    current_user: User = Depends(deps.get_current_user),
    turn_service: TurnService = Depends(get_turn_service),
    chat_service: ChatService = Depends(get_chat_service),
) -> SuccessEnvelope[TurnSelectSnapshotResponse]:
    select_result = await turn_service.select_candidate(
        current_user=current_user,
        turn_id=turn_id,
        candidate_no=req.candidate_no,
    )
    snapshot = await chat_service.get_turns_page(
        current_user=current_user,
        chat_id=uuid.UUID(select_result["chat_id"]),
        before_turn_id=None,
        limit=limit,
        include_learning_data=include_learning_data,
    )
    data = TurnSelectSnapshotResponse(
        select_result=TurnSelectResponse.model_validate(select_result),
        snapshot=snapshot,
    )
    return success_response(data, status_code=200)


@router.post("/{turn_id}/regen/stream")
async def regen_assistant_turn(
    turn_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: TurnService = Depends(get_turn_service),
) -> StreamingResponse:
    prep = await service.prepare_regen(
        current_user=current_user,
        assistant_turn_id=turn_id,
    )

    async def event_generator():
        async for item in service.stream_regen_with_prep(
            prep=prep,
            current_user=current_user,
        ):
            yield item

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/{turn_id}/continue/stream")
async def continue_assistant_turn(
    turn_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: TurnService = Depends(get_turn_service),
) -> StreamingResponse:
    prep = await service.prepare_continue(
        current_user=current_user,
        assistant_turn_id=turn_id,
    )

    async def event_generator():
        async for item in service.stream_continue_with_prep(
            prep=prep,
            current_user=current_user,
        ):
            yield item

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/{turn_id}/edit/stream", status_code=status.HTTP_200_OK)
async def edit_user_turn_and_stream_reply(
    turn_id: uuid.UUID,
    req: UserTurnEditStreamRequest,
    current_user: User = Depends(deps.get_current_user),
    service: TurnService = Depends(get_turn_service),
) -> StreamingResponse:
    prep = await service.prepare_user_edit_stream(
        current_user=current_user,
        user_turn_id=turn_id,
        content=req.content,
        images=req.images,
    )

    async def event_generator():
        async for item in service.stream_user_edit_with_prep(
            prep=prep,
            current_user=current_user,
        ):
            yield item

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/{turn_id}/feedback-card", response_model=SuccessEnvelope[FeedbackCardPayload])
async def create_feedback_card(
    turn_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: TurnService = Depends(get_turn_service),
) -> SuccessEnvelope[FeedbackCardPayload]:
    data = await service.generate_feedback_card(current_user=current_user, turn_id=turn_id)
    return success_response(data, status_code=200)
