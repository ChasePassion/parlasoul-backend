"""Add privacy-safe persistent realtime call telemetry.

Revision ID: 20260717_0040
Revises: 20260525_0039
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260717_0040"
down_revision = "20260525_0039"
branch_labels = None
depends_on = None


_TABLES = (
    "realtime_call_sessions",
    "realtime_call_events",
    "realtime_turn_metrics",
    "realtime_webrtc_stats_samples",
)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_user_scope ON {table}
          FOR ALL
          USING (
            current_setting('app.current_user_id', true) = 'system'
            OR user_id = NULLIF(
              NULLIF(current_setting('app.current_user_id', true), ''),
              'system'
            )::uuid
          )
          WITH CHECK (
            current_setting('app.current_user_id', true) = 'system'
            OR user_id = NULLIF(
              NULLIF(current_setting('app.current_user_id', true), ''),
              'system'
            )::uuid
          )
        """
    )


def upgrade() -> None:
    op.create_table(
        "realtime_call_sessions",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rtc_session_id", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=16), server_default="chat", nullable=False),
        sa.Column("scenario", sa.String(length=32), server_default="normal", nullable=False),
        sa.Column("experiment_tag", sa.String(length=64), nullable=True),
        sa.Column("client_build", sa.String(length=80), nullable=True),
        sa.Column("server_build", sa.String(length=80), nullable=True),
        sa.Column("browser_name", sa.String(length=16), server_default="unknown", nullable=False),
        sa.Column("browser_version", sa.String(length=32), nullable=True),
        sa.Column("os_name", sa.String(length=16), server_default="unknown", nullable=False),
        sa.Column("os_version", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="starting", nullable=False),
        sa.Column("failure_stage", sa.String(length=24), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("connected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("turn_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("interruption_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "safe_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('chat', 'lab')", name="realtime_call_sessions_mode_check"),
        sa.CheckConstraint(
            "scenario IN ('normal', 'slow_speaker', 'barge_in', 'network_switch', "
            "'mic_toggle', 'background_tab')",
            name="realtime_call_sessions_scenario_check",
        ),
        sa.CheckConstraint(
            "status IN ('starting', 'connected', 'completed', 'failed', 'cancelled')",
            name="realtime_call_sessions_status_check",
        ),
        sa.CheckConstraint(
            "failure_stage IS NULL OR failure_stage IN ('signaling', 'ice', 'media', "
            "'stt', 'llm', 'tts', 'playout', 'persistence', 'client', 'unknown')",
            name="realtime_call_sessions_failure_stage_check",
        ),
        sa.CheckConstraint(
            "browser_name IN ('chrome', 'edge', 'firefox', 'safari', 'other', 'unknown')",
            name="realtime_call_sessions_browser_name_check",
        ),
        sa.CheckConstraint(
            "os_name IN ('windows', 'macos', 'ios', 'android', 'linux', 'chromeos', "
            "'other', 'unknown')",
            name="realtime_call_sessions_os_name_check",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="realtime_call_sessions_duration_check",
        ),
        sa.CheckConstraint("turn_count >= 0", name="realtime_call_sessions_turn_count_check"),
        sa.CheckConstraint(
            "interruption_count >= 0",
            name="realtime_call_sessions_interruption_count_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(safe_config) = 'object'",
            name="realtime_call_sessions_safe_config_object_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(summary) = 'object'",
            name="realtime_call_sessions_summary_object_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint(
            "observation_id",
            "user_id",
            name="realtime_call_sessions_observation_user_uniq",
        ),
        sa.UniqueConstraint("rtc_session_id", name="realtime_call_sessions_rtc_session_uniq"),
    )
    op.create_index(
        "realtime_call_sessions_user_started_idx",
        "realtime_call_sessions",
        ["user_id", "started_at", "observation_id"],
    )
    op.create_index(
        "realtime_call_sessions_user_mode_started_idx",
        "realtime_call_sessions",
        ["user_id", "mode", "started_at", "observation_id"],
    )
    op.create_index(
        "realtime_call_sessions_chat_id_idx", "realtime_call_sessions", ["chat_id"]
    )
    op.create_index(
        "realtime_call_sessions_character_id_idx", "realtime_call_sessions", ["character_id"]
    )
    op.create_index(
        "realtime_call_sessions_ended_at_idx", "realtime_call_sessions", ["ended_at"]
    )
    op.create_index(
        "realtime_call_sessions_user_failed_idx",
        "realtime_call_sessions",
        ["user_id", "started_at"],
        postgresql_where=sa.text("status = 'failed'"),
    )

    op.create_table(
        "realtime_call_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=12), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("occurred_offset_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('client', 'server')", name="realtime_call_events_source_check"),
        sa.CheckConstraint(
            "stage IS NULL OR stage IN ('signaling', 'ice', 'media', 'stt', 'llm', "
            "'tts', 'playout', 'persistence', 'client', 'unknown')",
            name="realtime_call_events_stage_check",
        ),
        sa.CheckConstraint("seq >= 0", name="realtime_call_events_seq_check"),
        sa.CheckConstraint(
            "occurred_offset_ms >= 0", name="realtime_call_events_offset_check"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="realtime_call_events_payload_object_check",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "user_id"],
            ["realtime_call_sessions.observation_id", "realtime_call_sessions.user_id"],
            name="realtime_call_events_session_user_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_id",
            "source",
            "seq",
            name="realtime_call_events_session_source_seq_uniq",
        ),
    )
    op.create_index(
        "realtime_call_events_session_timeline_idx",
        "realtime_call_events",
        ["observation_id", "occurred_offset_ms", "id"],
    )
    op.create_index(
        "realtime_call_events_received_at_idx", "realtime_call_events", ["received_at"]
    )

    op.create_table(
        "realtime_turn_metrics",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("generation_id", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=20), server_default="completed", nullable=False),
        sa.Column("failure_stage", sa.String(length=24), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("speech_duration_ms", sa.Integer(), nullable=True),
        sa.Column("endpointing_latency_ms", sa.Integer(), nullable=True),
        sa.Column("stt_first_partial_latency_ms", sa.Integer(), nullable=True),
        sa.Column("stt_final_latency_ms", sa.Integer(), nullable=True),
        sa.Column("llm_ttft_ms", sa.Integer(), nullable=True),
        sa.Column("first_speakable_chunk_latency_ms", sa.Integer(), nullable=True),
        sa.Column("tts_ttfb_ms", sa.Integer(), nullable=True),
        sa.Column("first_audio_enqueued_latency_ms", sa.Integer(), nullable=True),
        sa.Column("server_first_audio_read_latency_ms", sa.Integer(), nullable=True),
        sa.Column("client_first_audio_played_latency_ms", sa.Integer(), nullable=True),
        sa.Column("ttfa_ms", sa.Integer(), nullable=True),
        sa.Column("barge_in_stop_latency_ms", sa.Integer(), nullable=True),
        sa.Column("playout_queue_peak_ms", sa.Integer(), nullable=True),
        sa.Column("pacer_underrun_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_word_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_word_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('completed', 'interrupted', 'failed', 'cancelled')",
            name="realtime_turn_metrics_outcome_check",
        ),
        sa.CheckConstraint(
            "failure_stage IS NULL OR failure_stage IN ('signaling', 'ice', 'media', "
            "'stt', 'llm', 'tts', 'playout', 'persistence', 'client', 'unknown')",
            name="realtime_turn_metrics_failure_stage_check",
        ),
        sa.CheckConstraint("turn_seq >= 0", name="realtime_turn_metrics_turn_seq_check"),
        sa.CheckConstraint(
            "pacer_underrun_count >= 0 AND input_char_count >= 0 AND input_word_count >= 0 "
            "AND output_char_count >= 0 AND output_word_count >= 0",
            name="realtime_turn_metrics_counts_check",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "user_id"],
            ["realtime_call_sessions.observation_id", "realtime_call_sessions.user_id"],
            name="realtime_turn_metrics_session_user_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_id",
            "turn_seq",
            name="realtime_turn_metrics_session_turn_seq_uniq",
        ),
        sa.UniqueConstraint(
            "observation_id",
            "generation_id",
            name="realtime_turn_metrics_session_generation_uniq",
        ),
    )
    op.create_index(
        "realtime_turn_metrics_user_created_idx",
        "realtime_turn_metrics",
        ["user_id", "created_at"],
    )

    op.create_table(
        "realtime_webrtc_stats_samples",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sample_seq", sa.BigInteger(), nullable=False),
        sa.Column("sampled_offset_ms", sa.BigInteger(), nullable=False),
        sa.Column("connection_state", sa.String(length=16), nullable=False),
        sa.Column("ice_connection_state", sa.String(length=16), nullable=False),
        sa.Column("data_channel_state", sa.String(length=12), nullable=True),
        sa.Column("local_candidate_type", sa.String(length=12), nullable=True),
        sa.Column("remote_candidate_type", sa.String(length=12), nullable=True),
        sa.Column("candidate_protocol", sa.String(length=12), nullable=True),
        sa.Column("address_family", sa.String(length=12), nullable=True),
        sa.Column("rtt_ms", sa.Float(), nullable=True),
        sa.Column("inbound_jitter_ms", sa.Float(), nullable=True),
        sa.Column("packet_loss_ratio", sa.Float(), nullable=True),
        sa.Column("inbound_packets_received_delta", sa.BigInteger(), nullable=True),
        sa.Column("inbound_packets_lost_delta", sa.BigInteger(), nullable=True),
        sa.Column("inbound_bytes_received_delta", sa.BigInteger(), nullable=True),
        sa.Column("outbound_packets_sent_delta", sa.BigInteger(), nullable=True),
        sa.Column("outbound_bytes_sent_delta", sa.BigInteger(), nullable=True),
        sa.Column("jitter_buffer_delay_ms", sa.Float(), nullable=True),
        sa.Column("concealed_ratio", sa.Float(), nullable=True),
        sa.Column("concealed_samples_delta", sa.BigInteger(), nullable=True),
        sa.Column("silent_concealed_samples_delta", sa.BigInteger(), nullable=True),
        sa.Column("audio_level", sa.Float(), nullable=True),
        sa.Column("codec_mime_type", sa.String(length=40), nullable=True),
        sa.Column("codec_clock_rate", sa.Integer(), nullable=True),
        sa.Column("control_queue_size", sa.Integer(), nullable=True),
        sa.Column("data_channel_buffered_amount", sa.BigInteger(), nullable=True),
        sa.Column("counter_reset", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("available_outgoing_bitrate_bps", sa.Float(), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sample_seq >= 0", name="realtime_webrtc_stats_sample_seq_check"),
        sa.CheckConstraint(
            "sampled_offset_ms >= 0", name="realtime_webrtc_stats_offset_check"
        ),
        sa.CheckConstraint(
            "connection_state IN ('new', 'connecting', 'connected', 'disconnected', "
            "'failed', 'closed', 'unknown')",
            name="realtime_webrtc_stats_connection_state_check",
        ),
        sa.CheckConstraint(
            "ice_connection_state IN ('new', 'checking', 'connected', 'completed', "
            "'failed', 'disconnected', 'closed', 'unknown')",
            name="realtime_webrtc_stats_ice_state_check",
        ),
        sa.CheckConstraint(
            "data_channel_state IS NULL OR data_channel_state IN "
            "('connecting', 'open', 'closing', 'closed', 'unknown')",
            name="realtime_webrtc_stats_data_channel_state_check",
        ),
        sa.CheckConstraint(
            "local_candidate_type IS NULL OR local_candidate_type IN "
            "('host', 'srflx', 'prflx', 'relay', 'unknown')",
            name="realtime_webrtc_stats_local_candidate_check",
        ),
        sa.CheckConstraint(
            "remote_candidate_type IS NULL OR remote_candidate_type IN "
            "('host', 'srflx', 'prflx', 'relay', 'unknown')",
            name="realtime_webrtc_stats_remote_candidate_check",
        ),
        sa.CheckConstraint(
            "candidate_protocol IS NULL OR candidate_protocol IN ('udp', 'tcp', 'tls', 'unknown')",
            name="realtime_webrtc_stats_protocol_check",
        ),
        sa.CheckConstraint(
            "address_family IS NULL OR address_family IN ('ipv4', 'ipv6', 'unknown')",
            name="realtime_webrtc_stats_address_family_check",
        ),
        sa.CheckConstraint(
            "audio_level IS NULL OR (audio_level >= 0 AND audio_level <= 1)",
            name="realtime_webrtc_stats_audio_level_check",
        ),
        sa.CheckConstraint(
            "packet_loss_ratio IS NULL OR (packet_loss_ratio >= 0 AND packet_loss_ratio <= 1)",
            name="realtime_webrtc_stats_packet_loss_ratio_check",
        ),
        sa.CheckConstraint(
            "concealed_ratio IS NULL OR (concealed_ratio >= 0 AND concealed_ratio <= 1)",
            name="realtime_webrtc_stats_concealed_ratio_check",
        ),
        sa.CheckConstraint(
            "(rtt_ms IS NULL OR rtt_ms >= 0) AND "
            "(inbound_jitter_ms IS NULL OR inbound_jitter_ms >= 0) AND "
            "(jitter_buffer_delay_ms IS NULL OR jitter_buffer_delay_ms >= 0) AND "
            "(concealed_samples_delta IS NULL OR concealed_samples_delta >= 0) AND "
            "(silent_concealed_samples_delta IS NULL OR silent_concealed_samples_delta >= 0) "
            "AND (control_queue_size IS NULL OR control_queue_size >= 0) AND "
            "(data_channel_buffered_amount IS NULL OR data_channel_buffered_amount >= 0)",
            name="realtime_webrtc_stats_nonnegative_check",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "user_id"],
            ["realtime_call_sessions.observation_id", "realtime_call_sessions.user_id"],
            name="realtime_webrtc_stats_session_user_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_id",
            "sample_seq",
            name="realtime_webrtc_stats_session_sample_seq_uniq",
        ),
    )
    op.create_index(
        "realtime_webrtc_stats_session_timeline_idx",
        "realtime_webrtc_stats_samples",
        ["observation_id", "sampled_offset_ms", "id"],
    )
    op.create_index(
        "realtime_webrtc_stats_received_at_idx",
        "realtime_webrtc_stats_samples",
        ["received_at"],
    )

    for table in _TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_user_scope ON {table}")
    op.drop_table("realtime_webrtc_stats_samples")
    op.drop_table("realtime_turn_metrics")
    op.drop_table("realtime_call_events")
    op.drop_table("realtime_call_sessions")
