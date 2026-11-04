"""remove UNLISTED visibility from application contract

Revision ID: 20260501_0034
Revises: 20260430_0033
Create Date: 2026-05-01 00:00:00.000000

The PostgreSQL enum types (visibility_t and chat_visibility_t) keep UNLISTED as
a valid value for backward compatibility, but all existing UNLISTED rows are
migrated to PRIVATE and the RLS policy allows PUBLIC and PRIVATE direct-link
reads. The application-layer enums in Python and TypeScript have already
removed UNLISTED, so no new UNLISTED data can be created.
"""

from __future__ import annotations

from alembic import op

revision = "20260501_0034"
down_revision = "20260430_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Migrate existing UNLISTED rows to PRIVATE
    op.execute(
        "UPDATE characters SET visibility = 'PRIVATE' WHERE visibility = 'UNLISTED'"
    )
    op.execute(
        "UPDATE chats SET visibility = 'PRIVATE' WHERE visibility = 'UNLISTED'"
    )

    # 2) Update RLS policy to remove UNLISTED reference
    op.execute("DROP POLICY IF EXISTS characters_select_scope ON characters")
    op.execute(
        "CREATE POLICY characters_select_scope ON characters"
        " FOR SELECT"
        " USING ("
        "   visibility IN ('PUBLIC', 'PRIVATE')"
        "   OR creator_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
        " )"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS characters_select_scope ON characters")
    op.execute(
        "CREATE POLICY characters_select_scope ON characters"
        " FOR SELECT"
        " USING ("
        "   visibility IN ('PUBLIC', 'UNLISTED')"
        "   OR creator_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
        " )"
    )
    # Note: data already migrated to PRIVATE is NOT restored to UNLISTED.
