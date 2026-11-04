"""Discover config router."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.response import success_response
from src.core.dependencies import get_discover_config_service
from src.schemas.common import SuccessEnvelope
from src.schemas.discover import DiscoverConfigResponse
from src.services.discover_config_service import DiscoverConfigService

router = APIRouter(prefix="/v1/discover", tags=["discover"])


@router.get("/config", response_model=SuccessEnvelope[DiscoverConfigResponse])
async def get_discover_config(
    service: DiscoverConfigService = Depends(get_discover_config_service),
) -> SuccessEnvelope[DiscoverConfigResponse]:
    """Return lightweight discover-page runtime config."""
    data = service.get_public_config()
    return success_response(data, status_code=200)
