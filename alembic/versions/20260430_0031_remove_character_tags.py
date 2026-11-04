"""remove character tags column

Revision ID: 20260430_0031
Revises: 20260428_0030
Create Date: 2026-04-30 00:31:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "20260430_0031"
down_revision = "20260428_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE characters DROP COLUMN IF EXISTS tags;")


def downgrade() -> None:
    op.execute("ALTER TABLE characters ADD COLUMN IF NOT EXISTS tags TEXT[] NULL;")
