"""Add character-forge sessions/messages and characters.voice_profile.

Revision ID: 20261105_0046
Revises: 20261107_0049
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261105_0046"
down_revision = "20261107_0049"
branch_labels = None
depends_on = None

_SAFE_USER_ID = (
    "NULLIF(NULLIF(current_setting('app.current_user_id', true), ''), 'system')::uuid"
)
_IS_SYSTEM = "current_setting('app.current_user_id', true) = 'system'"


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "voice_profile",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )

    op.create_table(
        "character_forge_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), server_default="新角色", nullable=False),
        sa.Column("active_character_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
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
            "status IN ('active', 'archived')",
            name="character_forge_sessions_status_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["active_character_id"], ["characters.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "character_forge_sessions_user_updated_idx",
        "character_forge_sessions",
        ["user_id", sa.text("updated_at DESC")],
    )

    op.create_table(
        "character_forge_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column(
            "ui_payloads",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool')",
            name="character_forge_messages_role_check",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["character_forge_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "seq",
            name="character_forge_messages_session_seq_uniq",
        ),
    )
    op.create_index(
        "character_forge_messages_session_seq_idx",
        "character_forge_messages",
        ["session_id", "seq"],
    )

    for table in ("character_forge_sessions", "character_forge_messages"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        f"""
        CREATE POLICY character_forge_sessions_owner ON character_forge_sessions
          FOR ALL
          USING ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
          WITH CHECK ({_IS_SYSTEM} OR user_id = {_SAFE_USER_ID})
        """
    )
    op.execute(
        f"""
        CREATE POLICY character_forge_messages_owner ON character_forge_messages
          FOR ALL
          USING (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM character_forge_sessions s
              WHERE s.id = character_forge_messages.session_id
                AND s.user_id = {_SAFE_USER_ID}
            )
          )
          WITH CHECK (
            {_IS_SYSTEM}
            OR EXISTS (
              SELECT 1 FROM character_forge_sessions s
              WHERE s.id = character_forge_messages.session_id
                AND s.user_id = {_SAFE_USER_ID}
            )
          )
        """
    )


def downgrade() -> None:
    for table in ("character_forge_messages", "character_forge_sessions"):
        op.execute(f"DROP POLICY IF EXISTS {table}_owner ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "character_forge_messages_session_seq_idx",
        table_name="character_forge_messages",
    )
    op.drop_table("character_forge_messages")
    op.drop_index(
        "character_forge_sessions_user_updated_idx",
        table_name="character_forge_sessions",
    )
    op.drop_table("character_forge_sessions")
    op.drop_column("characters", "voice_profile")
