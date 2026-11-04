"""Character management router (CRUD + Market)."""

import uuid

from fastapi import APIRouter, Depends, Query, status

from src.api import deps
from src.api.response import success_response
from src.core.dependencies import get_character_service
from src.db.models import User
from src.schemas.character import CharacterCreate, CharacterResponse, CharacterUpdate
from src.schemas.common import SuccessEnvelope
from src.services.character_service import CharacterService

router = APIRouter(prefix="/v1/characters", tags=["characters"])


@router.post("", response_model=SuccessEnvelope[CharacterResponse], status_code=status.HTTP_201_CREATED)
async def create_character(
    character_in: CharacterCreate,
    current_user: User = Depends(deps.get_current_user),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[CharacterResponse]:
    """Create a new character."""
    data = await service.create_character(character_in, current_user)
    return success_response(data, status_code=201)


@router.get("", response_model=SuccessEnvelope[list[CharacterResponse]])
async def get_my_characters(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(deps.get_current_user),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[list[CharacterResponse]]:
    """Get list of characters created by the current user."""
    data = await service.list_my_characters(current_user, skip=skip, limit=limit)
    return success_response(data, status_code=200)


@router.get("/market", response_model=SuccessEnvelope[list[CharacterResponse]])
async def get_market_characters(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[list[CharacterResponse]]:
    """Get list of public characters from the market. No login required."""
    data = await service.list_market_characters(skip=skip, limit=limit)
    return success_response(data, status_code=200)


@router.get("/{character_id}", response_model=SuccessEnvelope[CharacterResponse])
async def get_character(
    character_id: uuid.UUID,
    current_user: User | None = Depends(deps.get_optional_current_user),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[CharacterResponse]:
    """Get a specific character by ID.
    
    Authorization: Optional
    - Public characters: accessible by anyone
    - Private characters: accessible by anyone with direct link
    - Unpublished characters: only accessible by creator
    - Invalid token: 401
    """
    data = await service.get_character(character_id, current_user=current_user)
    return success_response(data, status_code=200)


@router.put("/{character_id}", response_model=SuccessEnvelope[CharacterResponse])
async def update_character(
    character_id: uuid.UUID,
    character_in: CharacterUpdate,
    current_user: User = Depends(deps.get_current_user),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[CharacterResponse]:
    """Update a character. Only creator can update."""
    data = await service.update_character(
        character_id,
        character_in=character_in,
        current_user=current_user,
    )
    return success_response(data, status_code=200)


@router.post("/{character_id}/unpublish", response_model=SuccessEnvelope[CharacterResponse])
async def unpublish_character(
    character_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_user),
    service: CharacterService = Depends(get_character_service),
) -> SuccessEnvelope[CharacterResponse]:
    """Unpublish a character. Only creator can unpublish."""
    data = await service.unpublish_character(character_id, current_user=current_user)
    return success_response(data, status_code=200)
