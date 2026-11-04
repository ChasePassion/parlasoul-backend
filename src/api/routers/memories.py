"""Memory management API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query

from src.api.deps import get_current_user
from src.api.response import success_response
from src.api.schemas import (
    ConsolidateResponse,
    ConsolidateRequest,
    DeleteResponse,
    ManageRequest,
    ManageResponse,
    ResetRequest,
    ResetResponse,
    SearchRequest,
    SearchResponse,
)
from src.core.dependencies import get_memory_service
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.services.memory_service import MemoryService

router = APIRouter(prefix="/v1/memories", tags=["memories"])


@router.post("/manage", response_model=SuccessEnvelope[ManageResponse])
async def manage_memories(
    request: ManageRequest,
    current_user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> SuccessEnvelope[ManageResponse]:
    data = await service.manage(request=request, current_user=current_user)
    return success_response(data, status_code=200)


@router.post("/search", response_model=SuccessEnvelope[SearchResponse])
async def search_memories(
    request: SearchRequest,
    current_user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> SuccessEnvelope[SearchResponse]:
    data = await service.search(request=request, current_user=current_user)
    return success_response(data, status_code=200)


@router.delete("/reset", response_model=SuccessEnvelope[ResetResponse])
async def reset_memories(
    request: ResetRequest,
    current_user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> SuccessEnvelope[ResetResponse]:
    data = await service.reset(request=request, current_user=current_user)
    return success_response(data, status_code=200)


@router.post("/consolidate", response_model=SuccessEnvelope[ConsolidateResponse])
async def consolidate_memories(
    request: ConsolidateRequest,
    current_user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> SuccessEnvelope[ConsolidateResponse]:
    data = await service.consolidate(request=request, current_user=current_user)
    return success_response(data, status_code=200)


@router.delete("/{memory_id}", response_model=SuccessEnvelope[DeleteResponse])
async def delete_memory(
    memory_id: int = Path(..., description="Memory ID to delete"),
    character_id: str = Query(..., description="Character ID for ownership verification"),
    current_user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> SuccessEnvelope[DeleteResponse]:
    data = await service.delete(
        memory_id=memory_id,
        character_id=character_id,
        current_user=current_user,
    )
    return success_response(data, status_code=200)
