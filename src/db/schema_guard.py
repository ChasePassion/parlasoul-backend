"""Database schema compatibility checks executed at application startup."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# Keep this list minimal and focused on fields that are required by runtime code.
_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "users": {
        "avatar_image_key",
        "display_name",
        "email_verified",
        "dodoCustomerId",
        "subscription_tier",
        "subscription_status",
        "subscription_product_id",
        "subscription_current_period_end",
    },
    "user_settings": {
        "auto_read_aloud_enabled",
        "memory_enabled",
        "proactive_enabled",
        "reply_card_enabled",
        "preferred_expression_bias_enabled",
        "timezone",
    },
    "subscription_webhook_events": {
        "id",
        "webhook_id",
        "event_type",
        "webhook_timestamp",
        "customer_id",
        "subscription_id",
        "product_id",
        "payload_json",
        "processing_status",
        "processed_at",
        "created_at",
    },
    "payment_orders": {
        "id",
        "user_id",
        "provider",
        "channel",
        "order_kind",
        "dodo_product_id",
        "tier",
        "duration_days",
        "base_price_minor",
        "base_price_currency",
        "billing_currency",
        "dodo_customer_id",
        "dodo_checkout_session_id",
        "dodo_payment_id",
        "dodo_refund_id",
        "checkout_url",
        "charged_total_minor",
        "charged_currency",
        "settlement_total_minor",
        "settlement_currency",
        "status",
        "paid_at",
        "refunded_at",
        "checkout_payload_json",
        "last_event_payload_json",
        "created_at",
        "updated_at",
    },
    "payment_webhook_events": {
        "id",
        "webhook_id",
        "event_type",
        "webhook_timestamp",
        "customer_id",
        "payment_id",
        "refund_id",
        "dodo_product_id",
        "payload_json",
        "processing_status",
        "processed_at",
        "created_at",
    },
    "user_access_passes": {
        "id",
        "user_id",
        "source_order_id",
        "source_type",
        "channel",
        "dodo_product_id",
        "tier",
        "starts_at",
        "ends_at",
        "status",
        "created_at",
        "updated_at",
    },
    "characters": {
        "avatar_image_key",
        "voice_provider",
        "voice_model",
        "voice_provider_voice_id",
        "voice_source_type",
        "voice_profile",
        "llm_preset_id",
        "unpublished_at",
    },
    "chats": {
        "origin",
        "title",
    },
    "proactive_character_preferences": {
        "user_id",
        "character_id",
        "enabled",
        "created_at",
        "updated_at",
    },
    "proactive_message_dispatches": {
        "id",
        "user_id",
        "slot_at",
        "slot_label",
        "timezone",
        "status",
        "selected_character_id",
        "attempt_count",
        "next_attempt_at",
        "lease_expires_at",
        "error_code",
        "error_message",
        "payload_json",
        "created_at",
        "updated_at",
        "completed_at",
    },
    "voice_profiles": {
        "id",
        "owner_user_id",
        "provider",
        "provider_voice_id",
        "source_type",
        "status",
        "provider_status",
        "provider_model",
        "display_name",
        "description",
        "avatar_image_key",
        "preview_text",
        "preview_audio_url",
        "language_tags",
        "metadata",
        "created_at",
        "updated_at",
    },
    "growth_user_stats": {
        "user_id",
        "current_natural_streak",
        "longest_natural_streak",
        "makeup_card_balance",
        "last_natural_signin_date",
        "last_rewarded_natural_streak",
        "created_at",
        "updated_at",
    },
    "growth_daily_stats": {
        "user_id",
        "stat_date",
        "user_signin_message_count",
        "total_message_count",
        "total_word_count",
        "is_natural_signed",
        "natural_signed_at",
        "is_makeup_signed",
        "makeup_signed_at",
        "popup_consumed_at",
        "created_at",
        "updated_at",
    },
    "growth_character_daily_stats": {
        "user_id",
        "character_id",
        "stat_date",
        "total_message_count",
        "total_word_count",
        "total_exchange_count",
        "last_chat_at",
        "created_at",
        "updated_at",
    },
    "growth_character_stats": {
        "user_id",
        "character_id",
        "total_message_count",
        "total_word_count",
        "total_exchange_count",
        "chatted_days_count",
        "last_chat_at",
        "created_at",
        "updated_at",
    },
    "growth_share_triggers": {
        "id",
        "user_id",
        "chat_id",
        "character_id",
        "trigger_kind",
        "trigger_key",
        "payload",
        "triggered_at",
        "consumed_at",
        "created_at",
    },
    "realtime_call_sessions": {
        "observation_id",
        "user_id",
        "rtc_session_id",
        "mode",
        "scenario",
        "status",
        "started_at",
        "ended_at",
        "safe_config",
        "summary",
    },
    "realtime_call_events": {
        "observation_id",
        "user_id",
        "source",
        "seq",
        "event_type",
        "occurred_offset_ms",
        "payload",
    },
    "realtime_turn_metrics": {
        "observation_id",
        "user_id",
        "turn_seq",
        "generation_id",
        "outcome",
        "ttfa_ms",
        "barge_in_stop_latency_ms",
    },
    "realtime_webrtc_stats_samples": {
        "observation_id",
        "user_id",
        "sample_seq",
        "sampled_offset_ms",
        "connection_state",
        "ice_connection_state",
        "rtt_ms",
        "packet_loss_ratio",
        "remote_inbound_fraction_lost",
        "remote_inbound_rtt_ms",
    },
}


async def assert_required_columns(conn: AsyncConnection) -> None:
    """Ensure critical runtime columns exist before serving traffic.

    Missing columns indicate unapplied migrations. We fail fast during startup
    so request handlers do not crash mid-transaction.
    """
    if conn.dialect.name != "postgresql":
        logger.info(
            "Skip schema compatibility check for non-PostgreSQL dialect: %s",
            conn.dialect.name,
        )
        return

    missing: list[str] = []
    for table_name, required_columns in _REQUIRED_COLUMNS.items():
        rows = await conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = ANY (current_schemas(true))
                  AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        )
        existing = {row[0] for row in rows}
        for column_name in sorted(required_columns - existing):
            missing.append(f"{table_name}.{column_name}")

    if missing:
        missing_list = ", ".join(missing)
        raise RuntimeError(
            f"Database schema is outdated; missing required columns: {missing_list}. Run `alembic upgrade head` before starting the API."
        )
