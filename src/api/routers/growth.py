"""Growth system APIs for Phase 5."""

from __future__ import annotations

from datetime import date
import uuid

from fastapi import APIRouter, Depends, Query, status

from src.api import deps
from src.api.response import no_content_response, success_response
from src.core.dependencies import get_growth_service
from src.core.exceptions import APIError
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.growth import (
    GrowthCalendarResponse,
    GrowthChatHeaderResponse,
    GrowthEntryRequest,
    GrowthEntryResponse,
    GrowthMakeUpRequest,
    GrowthMakeUpResponse,
    GrowthOverviewResponse,
    GrowthShareCardsPageResponse,
)
from src.services.growth_service import GrowthService

router = APIRouter(prefix="/v1/growth", tags=["growth"])


def _parse_optional_month(month: str | None) -> str | None:
    if month is None:
        return None
    if len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:7].isdigit():
        raise APIError(status_code=400, code="invalid_param", message="invalid request: month is invalid")
    return month


def _parse_optional_uuid(value: str | None, *, field_name: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise APIError(
            status_code=400,
            code="invalid_param",
            message=f"invalid request: {field_name} is invalid",
        ) from exc


def _parse_iso_date(value: str, *, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise APIError(
            status_code=400,
            code="invalid_param",
            message=f"invalid request: {field_name} is invalid",
        ) from exc


@router.post("/entry", response_model=SuccessEnvelope[GrowthEntryResponse])
async def consume_entry(
    request: GrowthEntryRequest | None = None,
    current_user: User = Depends(deps.get_current_user),
    service: GrowthService = Depends(get_growth_service),
) -> SuccessEnvelope[GrowthEntryResponse]:
    data = await service.consume_entry(
        current_user=current_user,
        calendar_month=request.calendar_month if request else None,
    )
    return success_response(data, status_code=200)


@router.get("/calendar", response_model=SuccessEnvelope[GrowthCalendarResponse])
async def get_calendar(
    month: str | None = Query(None),
    current_user: User = Depends(deps.get_current_user),
    service: GrowthService = Depends(get_growth_service),
) -> SuccessEnvelope[GrowthCalendarResponse]:
    data = await service.get_calendar(current_user=current_user, month=_parse_optional_month(month))
    return success_response(data, status_code=200)


@router.post("/make-up", response_model=SuccessEnvelope[GrowthMakeUpResponse])
async def apply_make_up(
    request: GrowthMakeUpRequest,
    current_user: User = Depends(deps.get_current_user),
    service: GrowthService = Depends(get_growth_service),
) -> SuccessEnvelope[GrowthMakeUpResponse]:
    data = await service.apply_makeup(
        current_user=current_user,
        target_date=_parse_iso_date(request.target_date, field_name="target_date"),
    )
    return success_response(data, status_code=200)


@router.get("/chats/{chat_id}/header", response_model=SuccessEnvelope[GrowthChatHeaderResponse])
async def get_chat_header(
    chat_id: str,
    current_user: User = Depends(deps.get_current_user),
    service: GrowthService = Depends(get_growth_service),
) -> SuccessEnvelope[GrowthChatHeaderResponse]:
    data = await service.get_chat_header(
        current_user=current_user,
        chat_id=_parse_optional_uuid(chat_id, field_name="chat_id"),
    )
    return success_response(data, status_code=200)


@router.get("/overview", response_model=SuccessEnvelope[GrowthOverviewResponse])
async def get_overview(
    focus_character_id: str | None = Query(None),
    current_user: User = Depends(deps.get_current_user),
    service: GrowthService = Depends(get_growth_service),
) -> SuccessEnvelope[GrowthOverviewResponse]:
    data = await service.get_overview(
        current_user=current_user,
        focus_character_id=_parse_optional_uuid(focus_character_id, field_name="focus_character_id"),
    )
    return success_response(data, status_code=200)


@router.get("/share-cards/pending", response_model=SuccessEnvelope[GrowthShareCardsPageResponse])
async def list_pending_share_cards(
    chat_id: str | None = Query(None),
    limit: int = Query(10, ge=1, le=20),
    current_user: User = Depends(deps.get_current_user),
    service: GrowthService = Depends(get_growth_service),
) -> SuccessEnvelope[GrowthShareCardsPageResponse]:
    data = await service.list_pending_share_cards(
        current_user=current_user,
        chat_id=_parse_optional_uuid(chat_id, field_name="chat_id"),
        limit=limit,
    )
    return success_response(data, status_code=200)


@router.post("/share-cards/{trigger_id}/consume", status_code=status.HTTP_204_NO_CONTENT)
async def consume_share_card(
    trigger_id: str,
    current_user: User = Depends(deps.get_current_user),
    service: GrowthService = Depends(get_growth_service),
):
    await service.consume_share_card(
        current_user=current_user,
        trigger_id=_parse_optional_uuid(trigger_id, field_name="trigger_id"),
    )
    return no_content_response()
