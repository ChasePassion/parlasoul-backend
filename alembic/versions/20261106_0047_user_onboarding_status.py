"""Add onboarding_status to users.

Revision ID: 20261106_0047
Revises: 20261104_0045
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261106_0047"
down_revision = "20261104_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing users become completed through the initial default; the default is
    # switched to pending in the same transaction, so users created after this
    # migration start as pending.
    op.add_column(
        "users",
        sa.Column(
            "onboarding_status",
            sa.String(length=32),
            nullable=False,
            server_default="completed",
        ),
    )
    op.alter_column("users", "onboarding_status", server_default="pending")
    op.create_check_constraint(
        "users_onboarding_status_check",
        "users",
        "onboarding_status IN ('pending', 'discover_done', 'completed', 'skipped')",
    )


def downgrade() -> None:
    op.drop_constraint("users_onboarding_status_check", "users", type_="check")
    op.drop_column("users", "onboarding_status")
