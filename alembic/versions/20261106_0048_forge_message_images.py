"""Add images to character forge messages.

Revision ID: 20261106_0048
Revises: 20261105_0046
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261106_0048"
down_revision = "20261105_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "character_forge_messages",
        sa.Column("images", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("character_forge_messages", "images")
