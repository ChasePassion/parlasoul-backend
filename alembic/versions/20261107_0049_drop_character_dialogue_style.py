"""Drop the unused characters.dialogue_style_id column.

Revision ID: 20261107_0049
Revises: 20261106_0047
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261107_0049"
down_revision = "20261106_0047"
branch_labels = None
depends_on = None

_DOWNGRADE_STYLE_IDS = (
    "clear_inquiry",
    "free_spirit",
    "poetic_reserve",
    "proud_resolve",
    "spring_breeze",
    "true_nature",
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE characters "
        "DROP CONSTRAINT IF EXISTS characters_dialogue_style_id_check"
    )
    op.drop_column("characters", "dialogue_style_id")


def downgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "dialogue_style_id",
            sa.String(length=40),
            nullable=False,
            server_default="true_nature",
        ),
    )
    op.create_check_constraint(
        "characters_dialogue_style_id_check",
        "characters",
        "dialogue_style_id IN ({ids})".format(
            ids=", ".join(f"'{item}'" for item in _DOWNGRADE_STYLE_IDS)
        ),
    )
