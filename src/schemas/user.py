"""User profile schemas for request/response models."""

from datetime import datetime
from typing import Literal

from sqlmodel import Field, SQLModel

from src.schemas.media import AvatarUrls
from src.schemas.payment import UserAccessPassResponse

SubscriptionTier = Literal["free", "plus", "pro"]
EffectiveEntitlementSource = Literal["none", "recurring_subscription", "one_time_pass"]


class UserProfileResponse(SQLModel):
    """Response model for user profile."""

    id: str
    email: str
    username: str | None = None
    email_verified: bool
    avatar_image_key: str | None = None
    avatar_urls: AvatarUrls | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class UserProfileUpdate(SQLModel):
    """Request model for updating user profile."""

    username: str | None = Field(None, min_length=2, max_length=50)
    avatar_image_key: str | None = None


class UserEntitlementFeatures(SQLModel):
    voice_clone: bool
    memory_feature: bool


class UserEntitlementSettings(SQLModel):
    memory_enabled: bool


class UserEntitlementsResponse(SQLModel):
    tier: SubscriptionTier
    subscription_status: str | None = None
    subscription_product_id: str | None = None
    current_period_end: datetime | None = None
    effective_source: EffectiveEntitlementSource = "none"
    effective_expires_at: datetime | None = None
    active_pass: UserAccessPassResponse | None = None
    features: UserEntitlementFeatures
    settings: UserEntitlementSettings
