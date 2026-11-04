"""Persist typed realtime pacer turn metrics.

Revision ID: 20260717_0043
Revises: 20260717_0042
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260717_0043"
down_revision = "20260717_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "realtime_turn_metrics",
        sa.Column("playout_queue_final_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "realtime_turn_metrics",
        sa.Column("server_playout_drained_latency_ms", sa.Integer(), nullable=True),
    )
    for name in (
        "pacer_partial_pad_count",
        "pacer_frames_read",
        "pacer_silence_frames",
        "pacer_rejected_write_count",
        "pacer_fadeout_count",
    ):
        op.add_column(
            "realtime_turn_metrics",
            sa.Column(name, sa.Integer(), server_default="0", nullable=False),
        )
    op.drop_constraint(
        "realtime_turn_metrics_counts_check", "realtime_turn_metrics", type_="check"
    )
    op.create_check_constraint(
        "realtime_turn_metrics_counts_check",
        "realtime_turn_metrics",
        "pacer_underrun_count >= 0 AND input_char_count >= 0 AND input_word_count >= 0 "
        "AND output_char_count >= 0 AND output_word_count >= 0 "
        "AND pacer_partial_pad_count >= 0 AND pacer_frames_read >= 0 "
        "AND pacer_silence_frames >= 0 AND pacer_rejected_write_count >= 0 "
        "AND pacer_fadeout_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "realtime_turn_metrics_counts_check", "realtime_turn_metrics", type_="check"
    )
    op.create_check_constraint(
        "realtime_turn_metrics_counts_check",
        "realtime_turn_metrics",
        "pacer_underrun_count >= 0 AND input_char_count >= 0 AND input_word_count >= 0 "
        "AND output_char_count >= 0 AND output_word_count >= 0",
    )
    for name in (
        "pacer_fadeout_count",
        "pacer_rejected_write_count",
        "pacer_silence_frames",
        "pacer_frames_read",
        "pacer_partial_pad_count",
    ):
        op.drop_column("realtime_turn_metrics", name)
    op.drop_column("realtime_turn_metrics", "server_playout_drained_latency_ms")
    op.drop_column("realtime_turn_metrics", "playout_queue_final_ms")
