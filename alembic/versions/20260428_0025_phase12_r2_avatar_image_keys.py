"""phase12_r2_avatar_image_keys

Revision ID: 20260428_0025
Revises: ef0c2ae315b0
Create Date: 2026-04-28 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260428_0025"
down_revision = "ef0c2ae315b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_image_key", sa.Text(), nullable=True))
    op.add_column("characters", sa.Column("avatar_image_key", sa.Text(), nullable=True))
    op.add_column("voice_profiles", sa.Column("avatar_image_key", sa.Text(), nullable=True))

    op.create_index(
        "users_avatar_image_key_idx",
        "users",
        ["avatar_image_key"],
        unique=False,
    )
    op.create_index(
        "characters_avatar_image_key_idx",
        "characters",
        ["avatar_image_key"],
        unique=False,
    )
    op.create_index(
        "voice_profiles_avatar_image_key_idx",
        "voice_profiles",
        ["avatar_image_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("voice_profiles_avatar_image_key_idx", table_name="voice_profiles")
    op.drop_index("characters_avatar_image_key_idx", table_name="characters")
    op.drop_index("users_avatar_image_key_idx", table_name="users")

    op.drop_column("voice_profiles", "avatar_image_key")
    op.drop_column("characters", "avatar_image_key")
    op.drop_column("users", "avatar_image_key")
