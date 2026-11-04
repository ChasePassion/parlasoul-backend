"""drop legacy local avatar columns

Revision ID: 20260428_0030
Revises: 20260428_0025
Create Date: 2026-04-28 00:30:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "20260428_0030"
down_revision = "20260428_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE characters DROP COLUMN IF EXISTS avatar_file_name;")
    op.execute("ALTER TABLE voice_profiles DROP COLUMN IF EXISTS avatar_file_name;")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE characters ADD COLUMN IF NOT EXISTS avatar_file_name VARCHAR(255) NULL;"
    )
    op.execute(
        "ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS avatar_file_name VARCHAR(255) NULL;"
    )
