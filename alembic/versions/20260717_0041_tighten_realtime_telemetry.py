"""Tighten realtime telemetry identifiers and add upstream RTCP metrics.

Revision ID: 20260717_0041
Revises: 20260717_0040
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260717_0041"
down_revision = "20260717_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "realtime_turn_metrics",
        "generation_id",
        existing_type=sa.String(length=64),
        type_=sa.BigInteger(),
        postgresql_using="generation_id::bigint",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "realtime_turn_metrics_generation_id_check",
        "realtime_turn_metrics",
        "generation_id >= 0",
    )
    op.create_check_constraint(
        "realtime_turn_metrics_error_code_check",
        "realtime_turn_metrics",
        "error_code IS NULL OR error_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
    )
    op.create_check_constraint(
        "realtime_call_sessions_rtc_session_id_check",
        "realtime_call_sessions",
        "rtc_session_id IS NULL OR rtc_session_id ~ '^rt_[a-z0-9]+$'",
    )
    op.create_check_constraint(
        "realtime_call_sessions_error_code_check",
        "realtime_call_sessions",
        "error_code IS NULL OR error_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
    )
    op.create_check_constraint(
        "realtime_call_events_event_type_check",
        "realtime_call_events",
        "event_type ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
    )
    op.create_check_constraint(
        "realtime_call_events_reason_code_check",
        "realtime_call_events",
        "reason_code IS NULL OR reason_code ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
    )

    op.add_column(
        "realtime_webrtc_stats_samples",
        sa.Column("remote_inbound_fraction_lost", sa.Float(), nullable=True),
    )
    op.add_column(
        "realtime_webrtc_stats_samples",
        sa.Column("remote_inbound_jitter_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "realtime_webrtc_stats_samples",
        sa.Column("remote_inbound_rtt_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "realtime_webrtc_stats_samples",
        sa.Column("remote_inbound_packets_lost_delta", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "realtime_webrtc_stats_remote_loss_ratio_check",
        "realtime_webrtc_stats_samples",
        "remote_inbound_fraction_lost IS NULL OR "
        "(remote_inbound_fraction_lost >= 0 AND remote_inbound_fraction_lost <= 1)",
    )
    op.drop_constraint(
        "realtime_webrtc_stats_nonnegative_check",
        "realtime_webrtc_stats_samples",
        type_="check",
    )
    op.create_check_constraint(
        "realtime_webrtc_stats_nonnegative_check",
        "realtime_webrtc_stats_samples",
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
    )


def downgrade() -> None:
    op.drop_constraint(
        "realtime_webrtc_stats_nonnegative_check",
        "realtime_webrtc_stats_samples",
        type_="check",
    )
    op.create_check_constraint(
        "realtime_webrtc_stats_nonnegative_check",
        "realtime_webrtc_stats_samples",
        "(rtt_ms IS NULL OR rtt_ms >= 0) AND "
        "(inbound_jitter_ms IS NULL OR inbound_jitter_ms >= 0) AND "
        "(jitter_buffer_delay_ms IS NULL OR jitter_buffer_delay_ms >= 0) AND "
        "(concealed_samples_delta IS NULL OR concealed_samples_delta >= 0) AND "
        "(silent_concealed_samples_delta IS NULL OR silent_concealed_samples_delta >= 0) AND "
        "(control_queue_size IS NULL OR control_queue_size >= 0) AND "
        "(data_channel_buffered_amount IS NULL OR data_channel_buffered_amount >= 0)",
    )
    op.drop_constraint(
        "realtime_webrtc_stats_remote_loss_ratio_check",
        "realtime_webrtc_stats_samples",
        type_="check",
    )
    op.drop_column("realtime_webrtc_stats_samples", "remote_inbound_packets_lost_delta")
    op.drop_column("realtime_webrtc_stats_samples", "remote_inbound_rtt_ms")
    op.drop_column("realtime_webrtc_stats_samples", "remote_inbound_jitter_ms")
    op.drop_column("realtime_webrtc_stats_samples", "remote_inbound_fraction_lost")
    op.drop_constraint(
        "realtime_call_events_reason_code_check", "realtime_call_events", type_="check"
    )
    op.drop_constraint(
        "realtime_call_events_event_type_check", "realtime_call_events", type_="check"
    )
    op.drop_constraint(
        "realtime_call_sessions_error_code_check", "realtime_call_sessions", type_="check"
    )
    op.drop_constraint(
        "realtime_call_sessions_rtc_session_id_check",
        "realtime_call_sessions",
        type_="check",
    )
    op.drop_constraint(
        "realtime_turn_metrics_error_code_check", "realtime_turn_metrics", type_="check"
    )
    op.drop_constraint(
        "realtime_turn_metrics_generation_id_check", "realtime_turn_metrics", type_="check"
    )
    op.alter_column(
        "realtime_turn_metrics",
        "generation_id",
        existing_type=sa.BigInteger(),
        type_=sa.String(length=64),
        postgresql_using="generation_id::text",
        existing_nullable=False,
    )
