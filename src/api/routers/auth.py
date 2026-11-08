from fastapi import APIRouter, Depends

from src.api import deps
from src.api.response import success_response
from src.db.models import User
from src.schemas.common import SuccessEnvelope
from src.schemas.user import UserProfileResponse
from src.services.media_url_service import build_avatar_urls

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.get("/me", response_model=SuccessEnvelope[UserProfileResponse])
async def read_users_me(
    current_user: User = Depends(deps.get_current_user),
) -> SuccessEnvelope[UserProfileResponse]:
    """
    Get current user's basic info (use /v1/users/me for full profile).
    """
    return success_response(
        {
            "id": str(current_user.id),
            "email": current_user.email,
            "username": current_user.username,
            "email_verified": current_user.email_verified,
            "avatar_image_key": current_user.avatar_image_key,
            "avatar_url": current_user.avatar_url,
            "avatar_urls": build_avatar_urls(current_user.avatar_image_key),
            "created_at": current_user.created_at,
            "last_login_at": current_user.last_login_at,
        },
        status_code=200,
    )
