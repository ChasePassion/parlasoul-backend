"""Expand realtime event stages independently from failure stages.

Revision ID: 20260717_0042
Revises: 20260717_0041
"""

from __future__ import annotations

from alembic import op

revision = "20260717_0042"
down_revision = "20260717_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "realtime_call_events_stage_check", "realtime_call_events", type_="check"
    )
    op.create_check_constraint(
        "realtime_call_events_stage_check",
        "realtime_call_events",
        "stage IS NULL OR stage IN ('session', 'signaling', 'ice', 'media', 'vad', "
        "'endpointing', 'stt', 'llm', 'tts', 'persistence', 'playout', "
        "'server_rtp_source', 'server_playout_queue', 'interruption', "
        "'state_machine', 'client', 'unknown')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "realtime_call_events_stage_check", "realtime_call_events", type_="check"
    )
    op.create_check_constraint(
        "realtime_call_events_stage_check",
        "realtime_call_events",
        "stage IS NULL OR stage IN ('signaling', 'ice', 'media', 'stt', 'llm', "
        "'tts', 'playout', 'persistence', 'client', 'unknown')",
    )
