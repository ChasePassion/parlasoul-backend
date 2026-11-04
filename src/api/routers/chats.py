"""Chat instance APIs (Chats -> Turns -> Candidates) with SSE streaming generation."""

import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse

from src.api import deps
from src.api.response import no_content_response, success_response, SSE_HEADERS
from src.core.dependencies import get_chat_service
from src.db.models import User
from src.schemas.chat import (
    CharacterBrief,
    ChatCreateRequest,
    ChatCreateResponse,
    ChatDetailResponse,
    ChatResponse,
    ChatsPageResponse,
    ChatStreamRequest,
    ChatUpdateRequest,
    TurnsPageResponse,
)
from src.schemas.common import SuccessEnvelope
from src.services.chat_service import ChatService

router = APIRouter(prefix="/v1/chats", tags=["chats"])


@router.get("", response_model=SuccessEnvelope[ChatsPageResponse])
async def list_chats(
    character_id: uuid.UUID = Query(..., description="Character ID"),
    cursor: str | None = Query(None, description="Opaque cursor"),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(deps.get_current_user),
    service: ChatService = Depends(get_chat_service),
) -> SuccessEnvelope[ChatsPageResponse]:
    """List current user's active chats for one character."""
    data = await service.list_chats_page(
        current_user=current_user,
        character_id=character_id,
        cursor=cursor,
        limit=limit,
    )
    return success_response(data, status_code=200)


@router.get("/recent", response_model=SuccessEnvelope[ChatDetailResponse | None])
async def get_recent_chat(
    character_id: uuid.UUID = Query(..., description="Character ID"),
    current_user: User = Depends(deps.get_current_user),
    service: ChatService = Depends(get_chat_service),
) -> SuccessEnvelope[ChatDetailResponse | None]:
    """Get the most recent ACTIVE chat for the current user + character."""
    data = await service.get_recent_chat_detail(current_user=current_user, character_id=character_id)
    return success_response(data, status_code=200)


@router.post("", response_model=SuccessEnvelope[ChatCreateResponse], status_code=status.HTTP_201_CREATED)
async def create_chat(
    chat_in: ChatCreateRequest,
    current_user: User = Depends(deps.get_current_user),
    service: ChatService = Depends(get_chat_service),
) -> SuccessEnvelope[ChatCreateResponse]:
    """Create a new chat instance; greeting_message (if any) is inserted into turns."""
    data = await service.create_chat(
        current_user=current_user,
        character_id=chat_in.character_id,
        meta=chat_in.meta,
    )
    return success_response(data, status_code=201)


@router.patch("/{chat_id}", response_model=SuccessEnvelope[ChatResponse])
async def update_chat(
    chat_id: uuid.UUID,
    chat_in: ChatUpdateRequest,
    current_user: User = Depends(deps.get_current_user),
    service: ChatService = Depends(get_chat_service),
) -> SuccessEnvelope[ChatResponse]:
    """Rename one chat."""
    data = await service.update_chat_title(
        current_user=current_user,
        chat_id=chat_id,
        title=chat_in.title,
    )
    return success_response(data, status_code=200)


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    chat_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: ChatService = Depends(get_chat_service),
):
    """Hard-delete one chat."""
    await service.delete_chat(current_user=current_user, chat_id=chat_id)
    return no_content_response()


@router.get("/{chat_id}/turns", response_model=SuccessEnvelope[TurnsPageResponse])
async def get_chat_turns(
    chat_id: uuid.UUID,
    before_turn_id: uuid.UUID | None = Query(
        None, description="Return turns earlier than before_turn_id along the active branch"
    ),
    limit: int = Query(20, ge=1, le=100),
    include_learning_data: bool = Query(
        True,
        description="Include sentence card and input transform metadata in candidate.extra",
    ),
    current_user: User = Depends(deps.get_current_user),
    service: ChatService = Depends(get_chat_service),
) -> SuccessEnvelope[TurnsPageResponse]:
    """Cursor pagination for turns along the currently active branch."""
    data = await service.get_turns_page(
        current_user=current_user,
        chat_id=chat_id,
        before_turn_id=before_turn_id,
        limit=limit,
        include_learning_data=include_learning_data,
    )
    return success_response(data, status_code=200)


@router.post("/{chat_id}/stream")
async def stream_chat(
    chat_id: uuid.UUID,
    req: ChatStreamRequest,
    current_user: User = Depends(deps.get_current_user),
    service: ChatService = Depends(get_chat_service),
) -> StreamingResponse:
    """Stream assistant generation for a chat; writes Turns/Candidates to DB."""

    # Prepare placeholder turns before streaming starts so API errors can be returned
    # as normal JSON error envelopes (instead of a broken SSE connection).
    prep = await service.prepare_stream(
        current_user=current_user,
        chat_id=chat_id,
        content=req.content,
        images=req.images,
    )

    async def event_generator():
        async for item in service.stream_chat_with_prep(
            prep=prep, current_user=current_user, content=req.content, images=req.images
        ):
            yield item

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/characters", response_model=SuccessEnvelope[list[CharacterBrief]])
async def get_sidebar_characters(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(deps.get_current_user),
    service: ChatService = Depends(get_chat_service),
) -> SuccessEnvelope[list[CharacterBrief]]:
    """Get current user's characters that already have chat history."""
    data = await service.list_sidebar_characters(
        current_user=current_user,
        skip=skip,
        limit=limit,
    )
    return success_response(data, status_code=200)
