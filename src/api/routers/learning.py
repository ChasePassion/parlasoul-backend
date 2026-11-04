"""Learning APIs for Phase 3 on-demand cards."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from src.api import deps
from src.api.response import success_response, SSE_HEADERS
from src.core.exceptions import APIError
from src.core.dependencies import get_turn_service
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.learning import (
    AssistantStreamRequest,
    ReplyCardPayload,
    WordCardCore,
    WordCardGenerateRequest,
    WordCardPayload,
)
from src.services.chat_learning_service import ChatLearningService
from src.services.turn_service import TurnService

router = APIRouter(prefix="/v1/learning", tags=["learning"])


@router.post("/word-card", response_model=SuccessEnvelope[WordCardPayload])
async def create_word_card(
    request: WordCardGenerateRequest,
    current_user: User = Depends(deps.get_current_user),
    service: ChatLearningService = Depends(deps.get_chat_learning_service),
) -> SuccessEnvelope[WordCardPayload]:
    selected_text = request.selected_text
    if not selected_text.strip():
        raise APIError(
            status_code=400,
            code="word_card_selection_invalid",
            message="invalid request: selected_text must not be empty",
        )
    if not service.contains_english(selected_text):
        raise APIError(
            status_code=400,
            code="word_card_selection_invalid",
            message="invalid request: selected_text must contain English text",
        )

    card_core = WordCardCore.model_validate(
        await service.generate_word_card(
            selected_text=selected_text,
            context_text=request.context_text,
        )
    )
    favorite_state = await service.resolve_favorite_state(
        kind="word_card",
        surface=card_core.surface,
        user_id=current_user.id,
    )
    data = WordCardPayload(
        word_card=service.attach_word_card_favorite(card_core, favorite_state),
    )
    return success_response(data, status_code=200)


@router.post(
    "/reply-card/candidates/{candidate_id}",
    response_model=SuccessEnvelope[ReplyCardPayload],
)
async def create_reply_card(
    candidate_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: TurnService = Depends(get_turn_service),
) -> SuccessEnvelope[ReplyCardPayload]:
    data = await service.generate_reply_card_for_candidate(
        current_user=current_user,
        candidate_id=candidate_id,
    )
    return success_response(data, status_code=200)


@router.post("/assistant/stream")
async def stream_learning_assistant(
    request: AssistantStreamRequest,
    current_user: User = Depends(deps.get_current_user),
    service: ChatLearningService = Depends(deps.get_chat_learning_service),
) -> StreamingResponse:
    question = request.question.strip()
    if not question:
        raise APIError(
            status_code=400,
            code="invalid_param",
            message="invalid request: question must not be empty",
        )

    async def event_generator():
        async for item in service.stream_assistant_reply(
            question=question,
            character_chat_context=request.character_chat_context,
            assistant_chat_context=request.assistant_chat_context,
            user_id=str(current_user.id),
            chat_id=str(request.chat_id) if request.chat_id else None,
        ):
            yield item

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
