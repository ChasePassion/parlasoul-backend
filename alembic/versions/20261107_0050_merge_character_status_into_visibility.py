"""Merge the characters market lifecycle into visibility.

``status`` (ACTIVE/UNPUBLISHED) and ``visibility`` (PUBLIC/PRIVATE) always
moved together: publishing set both, the market queried both, and access
control only ever consulted ``status``. The two fields disagreed for
ACTIVE+PRIVATE rows, so visibility becomes the single lifecycle field:

- PRIVATE: draft or withdrawn from the market, creator-only interactive use
- PUBLIC: listed on the public market

Revision ID: 20261107_0050
Revises: 20261106_0048
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261107_0050"
down_revision = "20261106_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Everything off the market folds into PRIVATE; ACTIVE+PUBLIC stays PUBLIC.
    op.execute("UPDATE characters SET visibility = 'PRIVATE' WHERE status = 'UNPUBLISHED'")
    op.drop_column("characters", "status")


def downgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("status", sa.String(length=20), nullable=True),
    )
    op.execute("UPDATE characters SET status = 'ACTIVE' WHERE visibility = 'PUBLIC'")
    op.execute("UPDATE characters SET status = 'UNPUBLISHED' WHERE visibility = 'PRIVATE'")
    op.alter_column(
        "characters",
        "status",
        existing_type=sa.String(length=20),
        nullable=False,
        server_default="ACTIVE",
    )
