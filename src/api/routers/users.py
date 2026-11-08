"""User profile management router."""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from src.api import deps
from src.api.response import success_response
from src.core.dependencies import (
    get_proactive_preference_service,
    get_subscription_service,
    get_user_service,
    get_user_settings_service,
)
from src.db.models import User
from src.schemas.character import CharacterResponse
from src.schemas.common import SuccessEnvelope
from src.schemas.proactive import (
    ProactiveCharacterPreferenceListResponse,
    ReplaceProactiveCharacterPreferencesRequest,
)
from src.schemas.user import (
    OnboardingStatusUpdate,
    UserEntitlementsResponse,
    UserProfileResponse,
    UserProfileUpdate,
)
from src.schemas.user_settings import UserSettingsResponse, UserSettingsUpdate
from src.services.proactive_preference_service import ProactivePreferenceService
from src.services.subscription_service import SubscriptionService
from src.services.user_service import UserService
from src.services.user_settings_service import UserSettingsService

router = APIRouter(prefix="/v1/users", tags=["users"])


@router.get("/me", response_model=SuccessEnvelope[UserProfileResponse])
async def get_my_profile(
    current_user: User = Depends(deps.get_current_user),
    service: UserService = Depends(get_user_service),
) -> JSONResponse:
    """Get current user's profile.

    Returns complete user profile including username and avatar.
    """
    data = await service.get_my_profile(current_user)
    return success_response(data, status_code=200)


@router.put("/me", response_model=SuccessEnvelope[UserProfileResponse])
async def update_my_profile(
    profile: UserProfileUpdate,
    current_user: User = Depends(deps.get_current_user),
    service: UserService = Depends(get_user_service),
) -> JSONResponse:
    """Update current user's profile.

    Allows updating username, avatar_image_key or avatar_url (preset avatar).
    At least one field must be provided; the two avatar fields are mutually exclusive.
    """
    data = await service.update_my_profile(profile, current_user=current_user)
    return success_response(data, status_code=200)


@router.patch("/me/onboarding", response_model=SuccessEnvelope[UserProfileResponse])
async def update_my_onboarding_status(
    payload: OnboardingStatusUpdate,
    current_user: User = Depends(deps.get_current_user),
    service: UserService = Depends(get_user_service),
) -> JSONResponse:
    """Update the current user's onboarding tour status."""
    data = await service.update_onboarding_status(payload, current_user=current_user)
    return success_response(data, status_code=200)


@router.get("/me/settings", response_model=SuccessEnvelope[UserSettingsResponse])
async def get_my_settings(
    current_user: User = Depends(deps.get_current_user),
    service: UserSettingsService = Depends(get_user_settings_service),
) -> JSONResponse:
    """Get current user's settings."""
    data = await service.get_my_settings(current_user=current_user)
    return success_response(data, status_code=200)


@router.patch("/me/settings", response_model=SuccessEnvelope[UserSettingsResponse])
async def update_my_settings(
    settings_update: UserSettingsUpdate,
    current_user: User = Depends(deps.get_current_user),
    service: UserSettingsService = Depends(get_user_settings_service),
) -> JSONResponse:
    """Patch current user's settings."""
    data = await service.update_my_settings(
        settings_update,
        current_user=current_user,
    )
    return success_response(data, status_code=200)


@router.get("/me/entitlements", response_model=SuccessEnvelope[UserEntitlementsResponse])
async def get_my_entitlements(
    current_user: User = Depends(deps.get_current_user),
    settings_service: UserSettingsService = Depends(get_user_settings_service),
    subscription_service: SubscriptionService = Depends(get_subscription_service),
) -> JSONResponse:
    user_settings = await settings_service.get_my_settings(current_user=current_user)
    data = await subscription_service.build_entitlements_payload(current_user, user_settings)
    return success_response(data, status_code=200)


@router.get(
    "/me/proactive-characters",
    response_model=SuccessEnvelope[ProactiveCharacterPreferenceListResponse],
)
async def get_my_proactive_characters(
    current_user: User = Depends(deps.get_current_user),
    service: ProactivePreferenceService = Depends(get_proactive_preference_service),
) -> JSONResponse:
    data = await service.list_my_preferences(current_user=current_user)
    return success_response(data, status_code=200)


@router.put(
    "/me/proactive-characters",
    response_model=SuccessEnvelope[ProactiveCharacterPreferenceListResponse],
)
async def replace_my_proactive_characters(
    request: ReplaceProactiveCharacterPreferencesRequest,
    current_user: User = Depends(deps.get_current_user),
    service: ProactivePreferenceService = Depends(get_proactive_preference_service),
) -> JSONResponse:
    data = await service.replace_enabled_character_ids(
        current_user=current_user,
        character_ids=request.character_ids,
    )
    return success_response(data, status_code=200)


# --- Creator Homepage: List characters by creator ---


@router.get("/{creator_id}/characters", response_model=SuccessEnvelope[list[CharacterResponse]])
async def get_creator_characters(
    creator_id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User | None = Depends(deps.get_optional_current_user),
    service: UserService = Depends(get_user_service),
) -> JSONResponse:
    """Get characters created by a specific user.

    Authorization: Optional
    - If viewer is the creator: returns all characters
    - Otherwise: returns only public characters
    - Invalid token: 401
    """
    data = await service.get_creator_characters(
        creator_id=creator_id,
        skip=skip,
        limit=limit,
        current_user=current_user,
    )
    return success_response(data, status_code=200)
