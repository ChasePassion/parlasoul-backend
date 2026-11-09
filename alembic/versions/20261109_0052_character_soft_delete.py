"""Soft delete for characters.

Revision ID: 20261109_0052
Revises: 20261107_0051
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261109_0052"
down_revision = "20261107_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "characters_deleted_at_idx",
        "characters",
        ["deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("characters_deleted_at_idx", table_name="characters")
    op.drop_column("characters", "deleted_at")
