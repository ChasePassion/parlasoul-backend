"""Add audios to character forge messages.

Revision ID: 20261107_0051
Revises: 20261107_0050
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261107_0051"
down_revision = "20261107_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "character_forge_messages",
        sa.Column("audios", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("character_forge_messages", "audios")
