"""Saved item APIs for chat learning favorites."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from src.api import deps
from src.api.response import no_content_response, success_response
from src.core.dependencies import get_saved_item_service
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.learning import SavedItemCreateRequest, SavedItemResponse, SavedItemsPage
from src.services.saved_item_service import SavedItemService

router = APIRouter(prefix="/v1/saved-items", tags=["saved-items"])


@router.post("", response_model=SuccessEnvelope[SavedItemResponse], status_code=status.HTTP_201_CREATED)
async def create_saved_item(
    request: SavedItemCreateRequest,
    current_user: User = Depends(deps.get_current_user),
    service: SavedItemService = Depends(get_saved_item_service),
) -> SuccessEnvelope[SavedItemResponse]:
    """Create one saved item."""
    data = await service.create_saved_item(request=request, current_user=current_user)
    return success_response(data, status_code=201)


@router.get("", response_model=SuccessEnvelope[SavedItemsPage])
async def list_saved_items(
    kind: str | None = Query(default=None),
    role_id: str | None = Query(default=None),
    chat_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(deps.get_current_user),
    service: SavedItemService = Depends(get_saved_item_service),
) -> SuccessEnvelope[SavedItemsPage]:
    """List saved items with cursor pagination."""
    data = await service.list_saved_items(
        current_user=current_user,
        kind=kind,
        role_id=role_id,
        chat_id=chat_id,
        cursor=cursor,
        limit=limit,
    )
    return success_response(data, status_code=200)


@router.delete("/{saved_item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_item(
    saved_item_id: str,
    current_user: User = Depends(deps.get_current_user),
    service: SavedItemService = Depends(get_saved_item_service),
):
    """Delete one saved item."""
    await service.delete_saved_item(saved_item_id=saved_item_id, current_user=current_user)
    return no_content_response()

