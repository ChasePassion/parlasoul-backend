"""phase17 proactive messaging foundation

Revision ID: 20260515_0038
Revises: 20260512_0037
Create Date: 2026-05-15 17:45:26.980992
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260515_0038"
down_revision = "20260512_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("proactive_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "user_settings",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'Asia/Shanghai'"),
        ),
    )
    op.add_column(
        "chats",
        sa.Column(
            "origin",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
    )
    op.create_check_constraint("chats_origin_check", "chats", "origin IN ('user', 'proactive')")

    op.create_table(
        "proactive_character_preferences",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "character_id"),
    )

    op.create_table(
        "proactive_message_dispatches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slot_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("slot_label", sa.String(length=20), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("selected_character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'retry_waiting', 'succeeded', 'skipped', 'failed')",
            name="proactive_message_dispatches_status_check",
        ),
        sa.ForeignKeyConstraint(["selected_character_id"], ["characters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "slot_at", name="proactive_message_dispatches_user_slot_uniq"),
    )
    op.create_index(
        "proactive_message_dispatches_status_next_attempt_idx",
        "proactive_message_dispatches",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "proactive_message_dispatches_user_created_at_idx",
        "proactive_message_dispatches",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("proactive_message_dispatches_user_created_at_idx", table_name="proactive_message_dispatches")
    op.drop_index("proactive_message_dispatches_status_next_attempt_idx", table_name="proactive_message_dispatches")
    op.drop_table("proactive_message_dispatches")
    op.drop_table("proactive_character_preferences")
    op.drop_constraint("chats_origin_check", "chats", type_="check")
    op.drop_column("chats", "origin")
    op.drop_column("user_settings", "timezone")
    op.drop_column("user_settings", "proactive_enabled")
