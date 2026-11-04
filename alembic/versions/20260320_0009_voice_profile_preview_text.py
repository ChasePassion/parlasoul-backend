"""Add preview_text to voice profiles."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260320_0009"
down_revision = "20260227_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("voice_profiles", sa.Column("preview_text", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE voice_profiles
        SET preview_text = 'Hello, this is my voice preview. I am ready to talk with you.'
        WHERE source_type = 'clone'
          AND (preview_text IS NULL OR btrim(preview_text) = '')
        """
    )


def downgrade() -> None:
    op.drop_column("voice_profiles", "preview_text")
