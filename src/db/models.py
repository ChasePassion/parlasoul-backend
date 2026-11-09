import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, Relationship, SQLModel

from src.core.config import settings
from src.core.llm_presets import LLM_PRESET_FREE, LLM_PRESET_IDS
from src.core.tts_registry import (
    DEFAULT_SYSTEM_TTS_MODEL,
    DEFAULT_SYSTEM_TTS_PROVIDER,
    DEFAULT_SYSTEM_TTS_VOICE_ID,
)


class Base(SQLModel):
    """Shared SQLModel base."""


# ============== Enums ==============


class VisibilityType(enum.StrEnum):
    """Visibility enum values.

    Note: the database uses two enum *types* (`visibility_t` for characters and
    `chat_visibility_t` for chats) but they share the same value set.
    """

    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class ChatType(enum.StrEnum):
    """Chat type enum."""

    ONE_ON_ONE = "ONE_ON_ONE"
    ROOM = "ROOM"


class ChatState(enum.StrEnum):
    """Chat state enum."""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ChatOriginType(enum.StrEnum):
    """Chat origin enum."""

    USER = "user"
    PROACTIVE = "proactive"


class AuthorType(enum.StrEnum):
    """Author type for turns."""

    USER = "USER"
    CHARACTER = "CHARACTER"
    SYSTEM = "SYSTEM"


class TurnState(enum.StrEnum):
    """Turn state enum."""

    OK = "OK"
    FILTERED = "FILTERED"
    DELETED = "DELETED"
    ERROR = "ERROR"


class VoiceSourceType(enum.StrEnum):
    """Voice profile source type enum."""

    SYSTEM = "system"
    CLONE = "clone"
    DESIGNED = "designed"
    IMPORTED = "imported"


class VoiceProfileStatus(enum.StrEnum):
    """Voice profile lifecycle status enum."""

    CREATING = "creating"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class GrowthShareTriggerKind(enum.StrEnum):
    """Growth share-card trigger kind."""

    DAILY_SIGNIN_COMPLETED = "daily_signin_completed"
    CHARACTER_MESSAGE_MILESTONE = "character_message_milestone"


class PaymentOrderStatus(enum.StrEnum):
    """Lifecycle status for one-time payment orders."""

    CREATED = "created"
    CHECKOUT_CREATED = "checkout_created"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class UserAccessPassStatus(enum.StrEnum):
    """Lifecycle status for one-time access passes."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    REVOKED = "revoked"


class PaymentWebhookEventStatus(enum.StrEnum):
    """Processing status for payment webhooks."""

    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    IGNORED_UNKNOWN_ORDER = "ignored_unknown_order"


class ProactiveDispatchStatus(enum.StrEnum):
    """Lifecycle status for proactive message dispatch queue rows."""

    QUEUED = "queued"
    PROCESSING = "processing"
    RETRY_WAITING = "retry_waiting"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


# ============== PostgreSQL Enum Types ==============
# These must match the existing enum types in the database

visibility_enum = PG_ENUM(
    "PUBLIC",
    "PRIVATE",
    name="visibility_t",
    create_type=False,
)

chat_visibility_enum = PG_ENUM(
    "PUBLIC",
    "PRIVATE",
    name="chat_visibility_t",
    create_type=False,
)

chat_type_enum = PG_ENUM(
    "ONE_ON_ONE",
    "ROOM",
    name="chat_type_t",
    create_type=False,
)

chat_state_enum = PG_ENUM(
    "ACTIVE",
    "ARCHIVED",
    name="chat_state_t",
    create_type=False,
)

author_type_enum = PG_ENUM(
    "USER",
    "CHARACTER",
    "SYSTEM",
    name="author_type_t",
    create_type=False,
)

turn_state_enum = PG_ENUM(
    "OK",
    "FILTERED",
    "DELETED",
    "ERROR",
    name="turn_state_t",
    create_type=False,
)


# ============== Models ==============


class User(Base, table=True):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "onboarding_status IN ('pending', 'discover_done', 'completed', 'skipped')",
            name="users_onboarding_status_check",
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    email: str = Field(sa_column=Column(String(255), unique=True, nullable=False))
    display_name: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
    username: str | None = Field(
        default=None, sa_column=Column(String(50), unique=True, nullable=True)
    )
    email_verified: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default="false")
    )
    dodo_customer_id: str | None = Field(
        default=None,
        sa_column=Column("dodoCustomerId", Text, unique=True, nullable=True),
    )
    subscription_tier: str = Field(
        default="free",
        sa_column=Column(String(20), nullable=False, server_default="free"),
    )
    subscription_status: str | None = Field(
        default=None, sa_column=Column(String(40), nullable=True)
    )
    subscription_product_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    subscription_current_period_end: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    avatar_url: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    avatar_image_key: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    last_login_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    onboarding_status: str = Field(
        default="pending",
        sa_column=Column(String(32), nullable=False, server_default="pending"),
    )

    characters: list["Character"] = Relationship(back_populates="creator")
    chats: list["Chat"] = Relationship(back_populates="user")
    voice_profiles: list["VoiceProfile"] = Relationship(back_populates="owner")
    saved_items: list["SavedItem"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class UserSettings(Base, table=True):
    __tablename__ = "user_settings"
    __table_args__ = (
        CheckConstraint(
            (
                f"message_font_size BETWEEN "
                f"{settings.USER_SETTINGS_MESSAGE_FONT_SIZE_MIN} "
                f"AND {settings.USER_SETTINGS_MESSAGE_FONT_SIZE_MAX}"
            ),
            name="user_settings_message_font_size_check",
        ),
        CheckConstraint(
            "display_mode IN ('concise', 'detailed')",
            name="user_settings_display_mode_check",
        ),
    )

    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    message_font_size: int = Field(
        default=settings.USER_SETTINGS_MESSAGE_FONT_SIZE_DEFAULT,
        sa_column=Column(
            SmallInteger,
            nullable=False,
            server_default=str(settings.USER_SETTINGS_MESSAGE_FONT_SIZE_DEFAULT),
        ),
    )
    display_mode: str = Field(
        default=settings.USER_SETTINGS_DISPLAY_MODE_DEFAULT,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=settings.USER_SETTINGS_DISPLAY_MODE_DEFAULT,
        ),
    )
    memory_enabled: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default="true"),
    )
    reply_card_enabled: bool = Field(
        default=settings.USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT,
        sa_column=Column(
            Boolean,
            nullable=False,
            server_default="true" if settings.USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT else "false",
        ),
    )
    mixed_input_auto_translate_enabled: bool = Field(
        default=settings.USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT,
        sa_column=Column(
            Boolean,
            nullable=False,
            server_default=(
                "true"
                if settings.USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT
                else "false"
            ),
        ),
    )
    auto_read_aloud_enabled: bool = Field(
        default=settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT,
        sa_column=Column(
            Boolean,
            nullable=False,
            server_default=(
                "true" if settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT else "false"
            ),
        ),
    )
    preferred_expression_bias_enabled: bool = Field(
        default=settings.USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT,
        sa_column=Column(
            Boolean,
            nullable=False,
            server_default=(
                "true"
                if settings.USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT
                else "false"
            ),
        ),
    )
    proactive_enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        sa_column=Column(String(64), nullable=False, server_default="Asia/Shanghai"),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class SubscriptionWebhookEvent(Base, table=True):
    __tablename__ = "subscription_webhook_events"
    __table_args__ = (
        UniqueConstraint("webhook_id", name="subscription_webhook_events_webhook_id_uniq"),
        Index(
            "subscription_webhook_events_customer_created_at_idx",
            "customer_id",
            "created_at",
        ),
        Index(
            "subscription_webhook_events_processing_status_created_at_idx",
            "processing_status",
            "created_at",
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    webhook_id: str = Field(sa_column=Column(Text, nullable=False))
    event_type: str = Field(sa_column=Column(String(80), nullable=False))
    webhook_timestamp: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    customer_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    subscription_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    product_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    payload_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    processing_status: str = Field(
        default="received",
        sa_column=Column(String(32), nullable=False, server_default="received"),
    )
    processed_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )


class PaymentOrder(Base, table=True):
    __tablename__ = "payment_orders"
    __table_args__ = (
        CheckConstraint("provider IN ('dodo')", name="payment_orders_provider_check"),
        CheckConstraint("channel IN ('wechat_pay')", name="payment_orders_channel_check"),
        CheckConstraint("order_kind IN ('one_time_pass')", name="payment_orders_kind_check"),
        CheckConstraint("tier IN ('plus', 'pro')", name="payment_orders_tier_check"),
        CheckConstraint(
            "status IN ('created', 'checkout_created', 'pending', 'succeeded', 'failed', "
            "'cancelled', 'refunded', 'expired')",
            name="payment_orders_status_check",
        ),
        UniqueConstraint(
            "dodo_checkout_session_id",
            name="payment_orders_dodo_session_uniq",
        ),
        UniqueConstraint(
            "dodo_payment_id",
            name="payment_orders_dodo_payment_uniq",
        ),
        Index("payment_orders_user_created_at_idx", "user_id", "created_at"),
        Index("payment_orders_user_status_idx", "user_id", "status"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    provider: str = Field(
        default="dodo",
        sa_column=Column(String(32), nullable=False, server_default="dodo"),
    )
    channel: str = Field(
        default="wechat_pay",
        sa_column=Column(String(32), nullable=False, server_default="wechat_pay"),
    )
    order_kind: str = Field(
        default="one_time_pass",
        sa_column=Column(String(32), nullable=False, server_default="one_time_pass"),
    )
    dodo_product_id: str = Field(sa_column=Column(Text, nullable=False))
    tier: str = Field(sa_column=Column(String(20), nullable=False))
    duration_days: int = Field(sa_column=Column(BigInteger, nullable=False))
    base_price_minor: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    base_price_currency: str | None = Field(default=None, sa_column=Column(String(8), nullable=True))
    billing_currency: str = Field(
        default="CNY",
        sa_column=Column(String(8), nullable=False, server_default="CNY"),
    )
    dodo_customer_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    dodo_checkout_session_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    dodo_payment_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    dodo_refund_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    checkout_url: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    charged_total_minor: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    charged_currency: str | None = Field(default=None, sa_column=Column(String(8), nullable=True))
    settlement_total_minor: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    settlement_currency: str | None = Field(default=None, sa_column=Column(String(8), nullable=True))
    status: str = Field(
        default=PaymentOrderStatus.CREATED.value,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=PaymentOrderStatus.CREATED.value,
        ),
    )
    paid_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    refunded_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    checkout_payload_json: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    last_event_payload_json: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class UserAccessPass(Base, table=True):
    __tablename__ = "user_access_passes"
    __table_args__ = (
        CheckConstraint("source_type IN ('one_time_payment')", name="user_access_passes_source_type_check"),
        CheckConstraint("channel IN ('wechat_pay')", name="user_access_passes_channel_check"),
        CheckConstraint("tier IN ('plus', 'pro')", name="user_access_passes_tier_check"),
        CheckConstraint(
            "status IN ('active', 'expired', 'refunded', 'revoked')",
            name="user_access_passes_status_check",
        ),
        UniqueConstraint("source_order_id", name="user_access_passes_source_order_uniq"),
        Index("user_access_passes_user_status_idx", "user_id", "status"),
        Index("user_access_passes_user_ends_at_idx", "user_id", "ends_at"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    source_order_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("payment_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    source_type: str = Field(
        default="one_time_payment",
        sa_column=Column(String(32), nullable=False, server_default="one_time_payment"),
    )
    channel: str = Field(
        default="wechat_pay",
        sa_column=Column(String(32), nullable=False, server_default="wechat_pay"),
    )
    dodo_product_id: str = Field(sa_column=Column(Text, nullable=False))
    tier: str = Field(sa_column=Column(String(20), nullable=False))
    starts_at: datetime = Field(sa_column=Column(TIMESTAMP(timezone=True), nullable=False))
    ends_at: datetime = Field(sa_column=Column(TIMESTAMP(timezone=True), nullable=False))
    status: str = Field(
        default=UserAccessPassStatus.ACTIVE.value,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=UserAccessPassStatus.ACTIVE.value,
        ),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class PaymentWebhookEvent(Base, table=True):
    __tablename__ = "payment_webhook_events"
    __table_args__ = (
        UniqueConstraint("webhook_id", name="payment_webhook_events_webhook_id_uniq"),
        Index(
            "payment_webhook_events_processing_status_created_at_idx",
            "processing_status",
            "created_at",
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    webhook_id: str = Field(sa_column=Column(Text, nullable=False))
    event_type: str = Field(sa_column=Column(String(80), nullable=False))
    webhook_timestamp: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    customer_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    payment_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    refund_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    dodo_product_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    payload_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    processing_status: str = Field(
        default=PaymentWebhookEventStatus.RECEIVED.value,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=PaymentWebhookEventStatus.RECEIVED.value,
        ),
    )
    processed_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )


class VoiceProfile(Base, table=True):
    __tablename__ = "voice_profiles"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_voice_id", name="voice_profiles_provider_voice_uniq"
        ),
        CheckConstraint(
            "source_type IN ('system', 'clone', 'designed', 'imported')",
            name="voice_profiles_source_type_check",
        ),
        CheckConstraint(
            "status IN ('creating', 'processing', 'ready', 'failed', 'deleting', 'deleted')",
            name="voice_profiles_status_check",
        ),
        Index("voice_profiles_owner_user_created_at_idx", "owner_user_id", "created_at"),
        Index("voice_profiles_provider_status_idx", "provider", "status"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    owner_user_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
    )
    provider: str = Field(sa_column=Column(String(40), nullable=False))
    provider_voice_id: str = Field(sa_column=Column(String(191), nullable=False))
    source_type: str = Field(sa_column=Column(String(20), nullable=False))
    status: str = Field(sa_column=Column(String(20), nullable=False))
    provider_status: str | None = Field(default=None, sa_column=Column(String(40), nullable=True))
    provider_model: str = Field(sa_column=Column(String(120), nullable=False))
    display_name: str = Field(sa_column=Column(String(80), nullable=False))
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    avatar_image_key: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    preview_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    preview_audio_url: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    language_tags: list[str] | None = Field(
        default=None, sa_column=Column(ARRAY(Text), nullable=True)
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSONB, nullable=False, server_default="{}"),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )

    owner: User | None = Relationship(back_populates="voice_profiles")


class Character(Base, table=True):
    __tablename__ = "characters"
    __table_args__ = (
        CheckConstraint(
            "voice_source_type IN ('system', 'clone', 'designed', 'imported')",
            name="characters_voice_source_type_check",
        ),
        CheckConstraint(
            f"llm_preset_id IN ({', '.join(repr(item) for item in sorted(LLM_PRESET_IDS))})",
            name="characters_llm_preset_id_check",
        ),
        Index(
            "characters_voice_binding_idx",
            "voice_provider",
            "voice_model",
            "voice_provider_voice_id",
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    identifier: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    name: str = Field(sa_column=Column(String(100), nullable=False))
    description: str = Field(sa_column=Column(Text, nullable=False))
    system_prompt: str = Field(sa_column=Column(Text, nullable=False))
    greeting_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    avatar_image_key: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    voice_provider: str = Field(
        default=DEFAULT_SYSTEM_TTS_PROVIDER,
        sa_column=Column(String(40), nullable=False, server_default=DEFAULT_SYSTEM_TTS_PROVIDER),
    )
    voice_model: str = Field(
        default=DEFAULT_SYSTEM_TTS_MODEL,
        sa_column=Column(String(120), nullable=False, server_default=DEFAULT_SYSTEM_TTS_MODEL),
    )
    voice_provider_voice_id: str = Field(
        default=DEFAULT_SYSTEM_TTS_VOICE_ID,
        sa_column=Column(String(191), nullable=False, server_default=DEFAULT_SYSTEM_TTS_VOICE_ID),
    )
    voice_source_type: str = Field(
        default=VoiceSourceType.SYSTEM.value,
        sa_column=Column(String(20), nullable=False, server_default=VoiceSourceType.SYSTEM.value),
    )
    voice_profile: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    llm_preset_id: str = Field(
        default=LLM_PRESET_FREE,
        sa_column=Column(String(40), nullable=False, server_default=LLM_PRESET_FREE),
    )
    unpublished_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    deleted_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    visibility: str = Field(
        default=VisibilityType.PRIVATE.value,
        sa_column=Column(visibility_enum, nullable=False),
    )
    creator_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=False
        ),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )

    creator: User | None = Relationship(back_populates="characters")
    chats: list["Chat"] = Relationship(back_populates="character")


class Chat(Base, table=True):
    __tablename__ = "chats"
    __table_args__ = (
        CheckConstraint("origin IN ('user', 'proactive')", name="chats_origin_check"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
    )
    character_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
        ),
    )
    type: str = Field(
        default=ChatType.ONE_ON_ONE.value, sa_column=Column(chat_type_enum, nullable=False)
    )
    state: str = Field(
        default=ChatState.ACTIVE.value, sa_column=Column(chat_state_enum, nullable=False)
    )
    visibility: str = Field(
        default=VisibilityType.PRIVATE.value,
        sa_column=Column(chat_visibility_enum, nullable=False),
    )
    origin: str = Field(
        default=ChatOriginType.USER.value,
        sa_column=Column(String(20), nullable=False, server_default=ChatOriginType.USER.value),
    )
    title: str = Field(
        default=settings.CHAT_TITLE_DEFAULT,
        sa_column=Column(
            String(settings.CHAT_TITLE_MAX_LENGTH),
            nullable=False,
            server_default=settings.CHAT_TITLE_DEFAULT,
        ),
    )
    last_turn_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    last_turn_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    last_turn_no: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    active_leaf_turn_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    last_read_turn_no: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    archived_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )

    user: User = Relationship(back_populates="chats")
    character: Character = Relationship(back_populates="chats")
    turns: list["Turn"] = Relationship(
        back_populates="chat",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class ProactiveCharacterPreference(Base, table=True):
    __tablename__ = "proactive_character_preferences"

    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    character_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("characters.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    enabled: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default="true"),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class ProactiveMessageDispatch(Base, table=True):
    __tablename__ = "proactive_message_dispatches"
    __table_args__ = (
        UniqueConstraint("user_id", "slot_at", name="proactive_message_dispatches_user_slot_uniq"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'retry_waiting', 'succeeded', 'skipped', 'failed')",
            name="proactive_message_dispatches_status_check",
        ),
        Index(
            "proactive_message_dispatches_status_next_attempt_idx",
            "status",
            "next_attempt_at",
        ),
        Index(
            "proactive_message_dispatches_user_created_at_idx",
            "user_id",
            "created_at",
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    slot_at: datetime = Field(sa_column=Column(TIMESTAMP(timezone=True), nullable=False))
    slot_label: str = Field(sa_column=Column(String(20), nullable=False))
    timezone: str = Field(sa_column=Column(String(64), nullable=False))
    status: str = Field(
        default=ProactiveDispatchStatus.QUEUED.value,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=ProactiveDispatchStatus.QUEUED.value,
        ),
    )
    selected_character_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("characters.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    next_attempt_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    error_code: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    payload_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )


class Turn(Base, table=True):
    """A turn represents a single speaking opportunity (bubble) in a chat."""

    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("chat_id", "turn_no", name="turns_chat_turn_no_uniq"),
        Index("turns_chat_parent_turn_idx", "chat_id", "parent_turn_id"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    chat_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
        ),
    )
    turn_no: int = Field(sa_column=Column(BigInteger, nullable=False))
    parent_turn_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    parent_candidate_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    author_type: str = Field(sa_column=Column(author_type_enum, nullable=False))
    author_user_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
    )
    author_character_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
        ),
    )
    state: str = Field(
        default=TurnState.OK.value, sa_column=Column(turn_state_enum, nullable=False)
    )
    is_proactive: bool = Field(default=False, sa_column=Column(Boolean, nullable=False))
    primary_candidate_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    meta: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )

    chat: Chat = Relationship(back_populates="turns")
    candidates: list["Candidate"] = Relationship(
        back_populates="turn",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Candidate(Base, table=True):
    """A candidate represents a specific text version for a turn."""

    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("turn_id", "candidate_no", name="candidates_turn_candidate_no_uniq"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    turn_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False
        ),
    )
    candidate_no: int = Field(sa_column=Column(BigInteger, nullable=False))
    content: str = Field(sa_column=Column(Text, nullable=False))
    model_type: str | None = Field(default=None, sa_column=Column(String(80), nullable=True))
    is_final: bool = Field(default=True, sa_column=Column(Boolean, nullable=False))
    rank: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )

    turn: Turn = Relationship(back_populates="candidates")


class SavedItem(Base, table=True):
    __tablename__ = "saved_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "kind",
            "display_surface",
            name="saved_items_user_kind_surface_uniq",
        ),
        Index("saved_items_user_created_at_idx", "user_id", "created_at"),
        Index("saved_items_user_kind_idx", "user_id", "kind"),
        Index("saved_items_user_source_chat_idx", "user_id", "source_chat_id"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
    )
    kind: str = Field(sa_column=Column(String(50), nullable=False))
    display_surface: str = Field(sa_column=Column(Text, nullable=False))
    display_zh: str = Field(sa_column=Column(Text, nullable=False))
    card: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    source_role_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    source_chat_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    source_message_id: str = Field(sa_column=Column(String(255), nullable=False))
    source_turn_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    source_candidate_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True),
    )
    source_meta: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )

    user: User = Relationship(back_populates="saved_items")


class GrowthUserStats(Base, table=True):
    __tablename__ = "growth_user_stats"

    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    current_natural_streak: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    longest_natural_streak: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    makeup_card_balance: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    last_natural_signin_date: date | None = Field(
        default=None, sa_column=Column(Date, nullable=True)
    )
    last_rewarded_natural_streak: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class GrowthDailyStats(Base, table=True):
    __tablename__ = "growth_daily_stats"
    __table_args__ = (
        UniqueConstraint("user_id", "stat_date", name="growth_daily_stats_user_date_uniq"),
        Index("growth_daily_stats_user_date_idx", "user_id", "stat_date"),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
    )
    stat_date: date = Field(sa_column=Column(Date, nullable=False))
    user_signin_message_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    total_message_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    total_word_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    is_natural_signed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    natural_signed_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    is_makeup_signed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    makeup_signed_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    popup_consumed_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class GrowthCharacterDailyStats(Base, table=True):
    __tablename__ = "growth_character_daily_stats"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "character_id",
            "stat_date",
            name="growth_character_daily_stats_user_character_date_uniq",
        ),
        Index("growth_character_daily_stats_user_date_idx", "user_id", "stat_date"),
        Index(
            "growth_character_daily_stats_user_character_date_idx",
            "user_id",
            "character_id",
            "stat_date",
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
    )
    character_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
        ),
    )
    stat_date: date = Field(sa_column=Column(Date, nullable=False))
    total_message_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    total_word_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    total_exchange_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    last_chat_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class GrowthCharacterStats(Base, table=True):
    __tablename__ = "growth_character_stats"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "character_id", name="growth_character_stats_user_character_uniq"
        ),
        Index(
            "growth_character_stats_user_sort_messages_idx",
            "user_id",
            "total_message_count",
            "chatted_days_count",
            "total_exchange_count",
            "character_id",
        ),
        Index(
            "growth_character_stats_user_sort_words_idx",
            "user_id",
            "total_word_count",
            "chatted_days_count",
            "total_exchange_count",
            "character_id",
        ),
        Index(
            "growth_character_stats_user_sort_days_idx",
            "user_id",
            "chatted_days_count",
            "total_message_count",
            "total_exchange_count",
            "character_id",
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
    )
    character_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False
        ),
    )
    total_message_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    total_word_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    total_exchange_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    chatted_days_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default="0"),
    )
    last_chat_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class GrowthShareTrigger(Base, table=True):
    __tablename__ = "growth_share_triggers"
    __table_args__ = (
        CheckConstraint(
            "trigger_kind IN ('daily_signin_completed', 'character_message_milestone')",
            name="growth_share_triggers_kind_check",
        ),
        UniqueConstraint("trigger_key", name="growth_share_triggers_trigger_key_uniq"),
        Index(
            "growth_share_triggers_user_pending_idx", "user_id", "consumed_at", "triggered_at", "id"
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
    )
    chat_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    character_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    trigger_kind: str = Field(sa_column=Column(String(50), nullable=False))
    trigger_key: str = Field(sa_column=Column(String(255), nullable=False))
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    triggered_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    consumed_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )


class RealtimeCallSession(Base, table=True):
    """Privacy-safe observability envelope for one realtime call attempt."""

    __tablename__ = "realtime_call_sessions"
    __table_args__ = (
        CheckConstraint("mode IN ('chat', 'lab')", name="realtime_call_sessions_mode_check"),
        CheckConstraint(
            "scenario IN ('normal', 'slow_speaker', 'barge_in', 'network_switch', "
            "'mic_toggle', 'background_tab')",
            name="realtime_call_sessions_scenario_check",
        ),
        CheckConstraint(
            "status IN ('starting', 'connected', 'completed', 'failed', 'cancelled')",
            name="realtime_call_sessions_status_check",
        ),
        CheckConstraint(
            "failure_stage IS NULL OR failure_stage IN "
            "('signaling', 'ice', 'media', 'stt', 'llm', 'tts', 'playout', "
            "'persistence', 'client', 'unknown')",
            name="realtime_call_sessions_failure_stage_check",
        ),
        CheckConstraint(
            "browser_name IN ('chrome', 'edge', 'firefox', 'safari', 'other', 'unknown')",
            name="realtime_call_sessions_browser_name_check",
        ),
        CheckConstraint(
            "os_name IN ('windows', 'macos', 'ios', 'android', 'linux', 'chromeos', "
            "'other', 'unknown')",
            name="realtime_call_sessions_os_name_check",
        ),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="realtime_call_sessions_duration_check"),
        CheckConstraint("turn_count >= 0", name="realtime_call_sessions_turn_count_check"),
        CheckConstraint(
            "interruption_count >= 0",
            name="realtime_call_sessions_interruption_count_check",
        ),
        CheckConstraint(
            "jsonb_typeof(safe_config) = 'object'",
            name="realtime_call_sessions_safe_config_object_check",
        ),
        CheckConstraint(
            "jsonb_typeof(summary) = 'object'",
            name="realtime_call_sessions_summary_object_check",
        ),
        CheckConstraint(
            "rtc_session_id IS NULL OR rtc_session_id ~ '^rt_[a-z0-9]+$'",
            name="realtime_call_sessions_rtc_session_id_check",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="realtime_call_sessions_error_code_check",
        ),
        UniqueConstraint(
            "observation_id",
            "user_id",
            name="realtime_call_sessions_observation_user_uniq",
        ),
        UniqueConstraint("rtc_session_id", name="realtime_call_sessions_rtc_session_uniq"),
        Index(
            "realtime_call_sessions_user_started_idx",
            "user_id",
            "started_at",
            "observation_id",
        ),
        Index(
            "realtime_call_sessions_user_mode_started_idx",
            "user_id",
            "mode",
            "started_at",
            "observation_id",
        ),
        Index("realtime_call_sessions_chat_id_idx", "chat_id"),
        Index("realtime_call_sessions_character_id_idx", "character_id"),
        Index("realtime_call_sessions_ended_at_idx", "ended_at"),
    )

    observation_id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    chat_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("chats.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    character_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("characters.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    rtc_session_id: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )
    mode: str = Field(default="chat", sa_column=Column(String(16), nullable=False))
    scenario: str = Field(default="normal", sa_column=Column(String(32), nullable=False))
    experiment_tag: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )
    client_build: str | None = Field(
        default=None,
        sa_column=Column(String(80), nullable=True),
    )
    server_build: str | None = Field(
        default=None,
        sa_column=Column(String(80), nullable=True),
    )
    browser_name: str = Field(default="unknown", sa_column=Column(String(16), nullable=False))
    browser_version: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )
    os_name: str = Field(default="unknown", sa_column=Column(String(16), nullable=False))
    os_version: str | None = Field(default=None, sa_column=Column(String(32), nullable=True))
    status: str = Field(default="starting", sa_column=Column(String(20), nullable=False))
    failure_stage: str | None = Field(
        default=None,
        sa_column=Column(String(24), nullable=True),
    )
    error_code: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    connected_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    ended_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    duration_ms: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    turn_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    interruption_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    safe_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    summary: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class RealtimeCallEvent(Base, table=True):
    """Low-frequency, content-free event on a realtime call timeline."""

    __tablename__ = "realtime_call_events"
    __table_args__ = (
        CheckConstraint("source IN ('client', 'server')", name="realtime_call_events_source_check"),
        CheckConstraint(
            "stage IS NULL OR stage IN ('session', 'signaling', 'ice', 'media', 'vad', "
            "'endpointing', 'stt', 'llm', 'tts', 'persistence', 'playout', "
            "'server_rtp_source', 'server_playout_queue', 'interruption', "
            "'state_machine', 'client', 'unknown')",
            name="realtime_call_events_stage_check",
        ),
        CheckConstraint("seq >= 0", name="realtime_call_events_seq_check"),
        CheckConstraint(
            "occurred_offset_ms >= 0",
            name="realtime_call_events_offset_check",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="realtime_call_events_payload_object_check",
        ),
        CheckConstraint(
            "event_type ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="realtime_call_events_event_type_check",
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="realtime_call_events_reason_code_check",
        ),
        ForeignKeyConstraint(
            ["observation_id", "user_id"],
            ["realtime_call_sessions.observation_id", "realtime_call_sessions.user_id"],
            name="realtime_call_events_session_user_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "observation_id",
            "source",
            "seq",
            name="realtime_call_events_session_source_seq_uniq",
        ),
        Index(
            "realtime_call_events_session_timeline_idx",
            "observation_id",
            "occurred_offset_ms",
            "id",
        ),
        Index("realtime_call_events_received_at_idx", "received_at"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True, nullable=False),
    )
    observation_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    user_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    source: str = Field(sa_column=Column(String(12), nullable=False))
    seq: int = Field(sa_column=Column(BigInteger, nullable=False))
    event_type: str = Field(sa_column=Column(String(80), nullable=False))
    stage: str | None = Field(default=None, sa_column=Column(String(24), nullable=True))
    reason_code: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    occurred_offset_ms: int = Field(sa_column=Column(BigInteger, nullable=False))
    received_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )


class RealtimeTurnMetric(Base, table=True):
    """Typed per-turn latency and quality summary without conversation text."""

    __tablename__ = "realtime_turn_metrics"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('completed', 'interrupted', 'failed', 'cancelled')",
            name="realtime_turn_metrics_outcome_check",
        ),
        CheckConstraint(
            "failure_stage IS NULL OR failure_stage IN ('signaling', 'ice', 'media', "
            "'stt', 'llm', 'tts', 'playout', 'persistence', 'client', 'unknown')",
            name="realtime_turn_metrics_failure_stage_check",
        ),
        CheckConstraint("turn_seq >= 0", name="realtime_turn_metrics_turn_seq_check"),
        CheckConstraint("generation_id >= 0", name="realtime_turn_metrics_generation_id_check"),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="realtime_turn_metrics_error_code_check",
        ),
        CheckConstraint(
            "pacer_underrun_count >= 0 AND input_char_count >= 0 AND input_word_count >= 0 "
            "AND output_char_count >= 0 AND output_word_count >= 0 "
            "AND pacer_partial_pad_count >= 0 AND pacer_frames_read >= 0 "
            "AND pacer_silence_frames >= 0 AND pacer_rejected_write_count >= 0 "
            "AND pacer_fadeout_count >= 0",
            name="realtime_turn_metrics_counts_check",
        ),
        ForeignKeyConstraint(
            ["observation_id", "user_id"],
            ["realtime_call_sessions.observation_id", "realtime_call_sessions.user_id"],
            name="realtime_turn_metrics_session_user_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "observation_id",
            "turn_seq",
            name="realtime_turn_metrics_session_turn_seq_uniq",
        ),
        UniqueConstraint(
            "observation_id",
            "generation_id",
            name="realtime_turn_metrics_session_generation_uniq",
        ),
        Index("realtime_turn_metrics_user_created_idx", "user_id", "created_at"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True, nullable=False),
    )
    observation_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    user_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    turn_seq: int = Field(sa_column=Column(Integer, nullable=False))
    generation_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    outcome: str = Field(default="completed", sa_column=Column(String(20), nullable=False))
    failure_stage: str | None = Field(default=None, sa_column=Column(String(24), nullable=True))
    error_code: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    speech_duration_ms: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    endpointing_latency_ms: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    stt_first_partial_latency_ms: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    stt_final_latency_ms: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    llm_ttft_ms: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    first_speakable_chunk_latency_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    tts_ttfb_ms: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    first_audio_enqueued_latency_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    server_first_audio_read_latency_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    client_first_audio_played_latency_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    ttfa_ms: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    barge_in_stop_latency_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    playout_queue_peak_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    playout_queue_final_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    server_playout_drained_latency_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    pacer_underrun_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    pacer_partial_pad_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    pacer_frames_read: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    pacer_silence_frames: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    pacer_rejected_write_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    pacer_fadeout_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    input_char_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    input_word_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    output_char_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    output_word_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class RealtimeWebRTCStatsSample(Base, table=True):
    """One normalized, address-free browser RTCPeerConnection stats sample."""

    __tablename__ = "realtime_webrtc_stats_samples"
    __table_args__ = (
        CheckConstraint("sample_seq >= 0", name="realtime_webrtc_stats_sample_seq_check"),
        CheckConstraint("sampled_offset_ms >= 0", name="realtime_webrtc_stats_offset_check"),
        CheckConstraint(
            "connection_state IN ('new', 'connecting', 'connected', 'disconnected', "
            "'failed', 'closed', 'unknown')",
            name="realtime_webrtc_stats_connection_state_check",
        ),
        CheckConstraint(
            "ice_connection_state IN ('new', 'checking', 'connected', 'completed', "
            "'failed', 'disconnected', 'closed', 'unknown')",
            name="realtime_webrtc_stats_ice_state_check",
        ),
        CheckConstraint(
            "local_candidate_type IS NULL OR local_candidate_type IN ('host', 'srflx', 'prflx', "
            "'relay', 'unknown')",
            name="realtime_webrtc_stats_local_candidate_check",
        ),
        CheckConstraint(
            "remote_candidate_type IS NULL OR remote_candidate_type IN ('host', 'srflx', "
            "'prflx', 'relay', 'unknown')",
            name="realtime_webrtc_stats_remote_candidate_check",
        ),
        CheckConstraint(
            "candidate_protocol IS NULL OR candidate_protocol IN ('udp', 'tcp', 'tls', 'unknown')",
            name="realtime_webrtc_stats_protocol_check",
        ),
        CheckConstraint(
            "address_family IS NULL OR address_family IN ('ipv4', 'ipv6', 'unknown')",
            name="realtime_webrtc_stats_address_family_check",
        ),
        CheckConstraint(
            "audio_level IS NULL OR (audio_level >= 0 AND audio_level <= 1)",
            name="realtime_webrtc_stats_audio_level_check",
        ),
        CheckConstraint(
            "packet_loss_ratio IS NULL OR (packet_loss_ratio >= 0 AND packet_loss_ratio <= 1)",
            name="realtime_webrtc_stats_packet_loss_ratio_check",
        ),
        CheckConstraint(
            "concealed_ratio IS NULL OR (concealed_ratio >= 0 AND concealed_ratio <= 1)",
            name="realtime_webrtc_stats_concealed_ratio_check",
        ),
        CheckConstraint(
            "remote_inbound_fraction_lost IS NULL OR "
            "(remote_inbound_fraction_lost >= 0 AND remote_inbound_fraction_lost <= 1)",
            name="realtime_webrtc_stats_remote_loss_ratio_check",
        ),
        CheckConstraint(
            "(rtt_ms IS NULL OR rtt_ms >= 0) AND "
            "(inbound_jitter_ms IS NULL OR inbound_jitter_ms >= 0) AND "
            "(jitter_buffer_delay_ms IS NULL OR jitter_buffer_delay_ms >= 0) AND "
            "(remote_inbound_jitter_ms IS NULL OR remote_inbound_jitter_ms >= 0) AND "
            "(remote_inbound_rtt_ms IS NULL OR remote_inbound_rtt_ms >= 0) AND "
            "(remote_inbound_packets_lost_delta IS NULL OR "
            "remote_inbound_packets_lost_delta >= 0) AND "
            "(concealed_samples_delta IS NULL OR concealed_samples_delta >= 0) AND "
            "(silent_concealed_samples_delta IS NULL OR silent_concealed_samples_delta >= 0) AND "
            "(control_queue_size IS NULL OR control_queue_size >= 0) AND "
            "(data_channel_buffered_amount IS NULL OR data_channel_buffered_amount >= 0)",
            name="realtime_webrtc_stats_nonnegative_check",
        ),
        CheckConstraint(
            "data_channel_state IS NULL OR data_channel_state IN "
            "('connecting', 'open', 'closing', 'closed', 'unknown')",
            name="realtime_webrtc_stats_data_channel_state_check",
        ),
        ForeignKeyConstraint(
            ["observation_id", "user_id"],
            ["realtime_call_sessions.observation_id", "realtime_call_sessions.user_id"],
            name="realtime_webrtc_stats_session_user_fkey",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "observation_id",
            "sample_seq",
            name="realtime_webrtc_stats_session_sample_seq_uniq",
        ),
        Index(
            "realtime_webrtc_stats_session_timeline_idx",
            "observation_id",
            "sampled_offset_ms",
            "id",
        ),
        Index("realtime_webrtc_stats_received_at_idx", "received_at"),
    )

    id: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True, nullable=False),
    )
    observation_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    user_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    sample_seq: int = Field(sa_column=Column(BigInteger, nullable=False))
    sampled_offset_ms: int = Field(sa_column=Column(BigInteger, nullable=False))
    connection_state: str = Field(sa_column=Column(String(16), nullable=False))
    ice_connection_state: str = Field(sa_column=Column(String(16), nullable=False))
    data_channel_state: str | None = Field(
        default=None,
        sa_column=Column(String(12), nullable=True),
    )
    local_candidate_type: str | None = Field(
        default=None,
        sa_column=Column(String(12), nullable=True),
    )
    remote_candidate_type: str | None = Field(
        default=None,
        sa_column=Column(String(12), nullable=True),
    )
    candidate_protocol: str | None = Field(
        default=None,
        sa_column=Column(String(12), nullable=True),
    )
    address_family: str | None = Field(default=None, sa_column=Column(String(12), nullable=True))
    rtt_ms: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    inbound_jitter_ms: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    packet_loss_ratio: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    remote_inbound_fraction_lost: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )
    remote_inbound_jitter_ms: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )
    remote_inbound_rtt_ms: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )
    remote_inbound_packets_lost_delta: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    inbound_packets_received_delta: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    inbound_packets_lost_delta: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    inbound_bytes_received_delta: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    outbound_packets_sent_delta: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    outbound_bytes_sent_delta: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    jitter_buffer_delay_ms: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )
    concealed_ratio: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    concealed_samples_delta: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    silent_concealed_samples_delta: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    audio_level: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    codec_mime_type: str | None = Field(default=None, sa_column=Column(String(40), nullable=True))
    codec_clock_rate: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    control_queue_size: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    data_channel_buffered_amount: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    counter_reset: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )
    available_outgoing_bitrate_bps: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )
    received_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )


# ============== Evaluation harness (phase18, CLI-only) ==============


class EvalRun(Base, table=True):
    """One evaluation run (case replay or aggregate-only metrics).

    CLI-only table with system-only RLS; never part of the API surface.
    Judge route is resolved and locked before the run starts (fail-closed);
    aggregate-only runs declare ``judge_route='none'`` and keep judge fields
    NULL.
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint(
            "content_mode IN ('none', 'redacted', 'full')",
            name="eval_runs_content_mode_check",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed', 'cancelled', 'no_data')",
            name="eval_runs_status_check",
        ),
        CheckConstraint("run_kind IN ('case', 'aggregate')", name="eval_runs_run_kind_check"),
        CheckConstraint("judge_route IN ('locked', 'none')", name="eval_runs_judge_route_check"),
        CheckConstraint("case_count >= 0", name="eval_runs_case_count_check"),
        CheckConstraint(
            "completed_case_count >= 0", name="eval_runs_completed_case_count_check"
        ),
        CheckConstraint("failed_case_count >= 0", name="eval_runs_failed_case_count_check"),
        CheckConstraint(
            "cancelled_case_count >= 0", name="eval_runs_cancelled_case_count_check"
        ),
        CheckConstraint("metric_count >= 0", name="eval_runs_metric_count_check"),
        CheckConstraint("metric_error_count >= 0", name="eval_runs_metric_error_count_check"),
        CheckConstraint(
            "cancelled_metric_count >= 0", name="eval_runs_cancelled_metric_count_check"
        ),
        CheckConstraint(
            "run_kind <> 'aggregate' OR (case_count = 0 AND completed_case_count = 0 "
            "AND failed_case_count = 0 AND cancelled_case_count = 0)",
            name="eval_runs_aggregate_no_case_counts_check",
        ),
        CheckConstraint(
            "run_kind <> 'case' OR (metric_count = 0 AND metric_error_count = 0 "
            "AND cancelled_metric_count = 0)",
            name="eval_runs_case_no_metric_counts_check",
        ),
        CheckConstraint(
            "judge_route <> 'locked' OR (effective_judge_provider IS NOT NULL "
            "AND effective_judge_model IS NOT NULL AND rubric_version IS NOT NULL "
            "AND prompt_hash IS NOT NULL)",
            name="eval_runs_locked_route_fields_check",
        ),
        CheckConstraint(
            "judge_route <> 'none' OR (effective_judge_provider IS NULL "
            "AND effective_judge_model IS NULL)",
            name="eval_runs_no_route_fields_check",
        ),
        CheckConstraint(
            "dataset_fingerprint ~ '^[0-9a-f]{64}$'",
            name="eval_runs_dataset_fingerprint_check",
        ),
        CheckConstraint(
            "prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$'",
            name="eval_runs_prompt_hash_check",
        ),
        CheckConstraint(
            "jsonb_typeof(selection_json) = 'object' AND jsonb_typeof(config_json) = 'object' "
            "AND jsonb_typeof(summary_json) = 'object' "
            "AND jsonb_typeof(generation_params) = 'object'",
            name="eval_runs_json_object_check",
        ),
        Index("eval_runs_status_created_idx", "status", text("created_at DESC")),
        Index(
            "eval_runs_compare_idx",
            "dataset_fingerprint",
            "effective_judge_provider",
            "effective_judge_model",
            "rubric_version",
            text("created_at DESC"),
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    name: str = Field(sa_column=Column(String(200), nullable=False))
    dataset_id: str = Field(sa_column=Column(String(120), nullable=False))
    dataset_version: str = Field(sa_column=Column(String(40), nullable=False))
    dataset_fingerprint: str = Field(sa_column=Column(String(64), nullable=False))
    dataset_path: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    runner_version: str = Field(sa_column=Column(String(80), nullable=False))
    git_revision: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    owner_user_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    run_kind: str = Field(sa_column=Column(String(10), nullable=False))
    judge_route: str = Field(default="locked", sa_column=Column(String(8), nullable=False))
    requested_judge_provider: str | None = Field(
        default=None, sa_column=Column(String(40), nullable=True)
    )
    requested_judge_model: str | None = Field(
        default=None, sa_column=Column(String(120), nullable=True)
    )
    effective_judge_provider: str | None = Field(
        default=None, sa_column=Column(String(40), nullable=True)
    )
    effective_judge_model: str | None = Field(
        default=None, sa_column=Column(String(120), nullable=True)
    )
    rubric_version: str | None = Field(default=None, sa_column=Column(String(80), nullable=True))
    prompt_template_version: str | None = Field(
        default=None, sa_column=Column(String(80), nullable=True)
    )
    prompt_hash: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    content_mode: str = Field(default="none", sa_column=Column(String(12), nullable=False))
    artifact_dir: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status: str = Field(default="running", sa_column=Column(String(16), nullable=False))
    case_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    completed_case_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    failed_case_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    cancelled_case_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    metric_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    metric_error_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    cancelled_metric_count: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default="0")
    )
    retention_until: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    seed: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    generation_params: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    selection_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    config_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    summary_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    started_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )

    results: list["EvalResult"] = Relationship(
        back_populates="run",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class EvalResult(Base, table=True):
    """One evaluated case inside a case run.

    ``status`` tracks runner completion; ``response_status`` tracks whether the
    generation/Judge response is usable as a valid score. ``content_mode``
    mirrors the owning run; raw text columns stay NULL in ``none`` mode.
    """

    __tablename__ = "eval_results"
    __table_args__ = (
        CheckConstraint(
            "runner IN ('chat_replay', 'candidate_replay', 'pairwise_replay', "
            "'realtime_functional')",
            name="eval_results_runner_check",
        ),
        CheckConstraint(
            "status IN ('completed', 'failed', 'skipped', 'no_data', 'cancelled')",
            name="eval_results_status_check",
        ),
        CheckConstraint(
            "response_status IN ('valid', 'runner_error', 'judge_timeout', 'judge_error', "
            "'judge_parse_failed', 'judge_inconsistency', 'skipped', 'no_data', 'cancelled')",
            name="eval_results_response_status_check",
        ),
        CheckConstraint(
            "content_mode IN ('none', 'redacted', 'full')",
            name="eval_results_content_mode_check",
        ),
        CheckConstraint(
            "pairwise_winner IS NULL OR pairwise_winner IN ('a', 'b', 'tie')",
            name="eval_results_pairwise_winner_check",
        ),
        CheckConstraint("attempt >= 1", name="eval_results_attempt_check"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="eval_results_duration_check"
        ),
        CheckConstraint(
            "char_length(judge_rationale) <= 4000", name="eval_results_rationale_len_check"
        ),
        CheckConstraint(
            "char_length(error_detail_safe) <= 1000", name="eval_results_error_detail_len_check"
        ),
        CheckConstraint(
            "content_mode <> 'none' OR (input_text IS NULL AND actual_output_text IS NULL)",
            name="eval_results_none_mode_no_text_check",
        ),
        CheckConstraint(
            "runner = 'pairwise_replay' OR (pairwise_winner IS NULL AND swap_consistent IS NULL "
            "AND variant_a_ref IS NULL AND variant_b_ref IS NULL "
            "AND variant_a_generation_provider IS NULL AND variant_b_generation_provider IS NULL)",
            name="eval_results_pairwise_fields_runner_check",
        ),
        CheckConstraint(
            "jsonb_typeof(dimension_scores_json) = 'object' "
            "AND jsonb_typeof(rule_metrics_json) = 'object'",
            name="eval_results_json_object_check",
        ),
        ForeignKeyConstraint(
            ["run_id"], ["eval_runs.id"], name="eval_results_run_fkey", ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["source_candidate_id"],
            ["candidates.id"],
            name="eval_results_source_candidate_fkey",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            name="eval_results_character_fkey",
            ondelete="SET NULL",
        ),
        UniqueConstraint("run_id", "case_id", "runner", name="eval_results_run_case_runner_uniq"),
        Index("eval_results_run_case_idx", "run_id", "case_id"),
        Index("eval_results_status_created_idx", "status", text("created_at DESC")),
        Index("eval_results_character_created_idx", "character_id", text("created_at DESC")),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    run_id: uuid.UUID = Field(sa_column=Column(PGUUID(as_uuid=True), nullable=False))
    case_id: str = Field(sa_column=Column(String(120), nullable=False))
    runner: str = Field(sa_column=Column(String(40), nullable=False))
    source_candidate_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    character_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    owner_user_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )
    status: str = Field(sa_column=Column(String(16), nullable=False))
    response_status: str = Field(sa_column=Column(String(24), nullable=False))
    attempt: int = Field(default=1, sa_column=Column(Integer, nullable=False, server_default="1"))
    started_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    duration_ms: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    content_mode: str = Field(default="none", sa_column=Column(String(12), nullable=False))
    input_ref: str = Field(sa_column=Column(String(200), nullable=False))
    output_ref: str = Field(sa_column=Column(String(200), nullable=False))
    input_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    actual_output_text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    dimension_scores_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    rule_metrics_json: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    judge_rationale: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    judge_provider: str | None = Field(default=None, sa_column=Column(String(40), nullable=True))
    judge_model: str | None = Field(default=None, sa_column=Column(String(120), nullable=True))
    rubric_version: str | None = Field(default=None, sa_column=Column(String(80), nullable=True))
    prompt_hash: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    generation_provider: str | None = Field(
        default=None, sa_column=Column(String(40), nullable=True)
    )
    generation_model: str | None = Field(
        default=None, sa_column=Column(String(120), nullable=True)
    )
    generation_model_type: str | None = Field(
        default=None, sa_column=Column(String(120), nullable=True)
    )
    pairwise_winner: str | None = Field(default=None, sa_column=Column(String(4), nullable=True))
    swap_consistent: bool | None = Field(default=None, sa_column=Column(Boolean, nullable=True))
    variant_a_ref: str | None = Field(default=None, sa_column=Column(String(200), nullable=True))
    variant_b_ref: str | None = Field(default=None, sa_column=Column(String(200), nullable=True))
    variant_a_generation_provider: str | None = Field(
        default=None, sa_column=Column(String(40), nullable=True)
    )
    variant_a_generation_model: str | None = Field(
        default=None, sa_column=Column(String(120), nullable=True)
    )
    variant_a_generation_model_type: str | None = Field(
        default=None, sa_column=Column(String(120), nullable=True)
    )
    variant_b_generation_provider: str | None = Field(
        default=None, sa_column=Column(String(40), nullable=True)
    )
    variant_b_generation_model: str | None = Field(
        default=None, sa_column=Column(String(120), nullable=True)
    )
    variant_b_generation_model_type: str | None = Field(
        default=None, sa_column=Column(String(120), nullable=True)
    )
    error_code: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    error_detail_safe: str | None = Field(
        default=None, sa_column=Column(String(1000), nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )

    run: EvalRun | None = Relationship(back_populates="results")


class CharacterForgeSession(Base, table=True):
    """A persisted conversation with the character-forge agent."""

    __tablename__ = "character_forge_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="character_forge_sessions_status_check",
        ),
        Index(
            "character_forge_sessions_user_updated_idx",
            "user_id",
            text("updated_at DESC"),
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    title: str = Field(
        default="新角色",
        sa_column=Column(Text, nullable=False, server_default="新角色"),
    )
    active_character_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("characters.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    status: str = Field(
        default="active",
        sa_column=Column(String(20), nullable=False, server_default="active"),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
    )


class CharacterForgeMessage(Base, table=True):
    """A single message inside a character-forge session.

    ``role='tool'`` rows keep the raw OpenAI tool result so the agent loop can
    be replayed after a page refresh. ``ui_payloads`` stores the frontend cards
    (voice preview, created character) so they survive a reload as well.
    """

    __tablename__ = "character_forge_messages"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "seq",
            name="character_forge_messages_session_seq_uniq",
        ),
        CheckConstraint(
            "role IN ('user', 'assistant', 'tool')",
            name="character_forge_messages_role_check",
        ),
        Index(
            "character_forge_messages_session_seq_idx",
            "session_id",
            "seq",
        ),
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    session_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("character_forge_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    seq: int = Field(sa_column=Column(BigInteger, nullable=False))
    role: str = Field(sa_column=Column(String(20), nullable=False))
    content: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    tool_call_id: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    tool_name: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    ui_payloads: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="[]"),
    )
    images: list[dict[str, Any]] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    audios: list[dict[str, Any]] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False),
    )
