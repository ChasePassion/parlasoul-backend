"""Switch default MiniMax TTS model from speech-2.8-hd to speech-2.8-turbo.

Revision ID: 20260512_0037
Revises: 20260509_0036
Create Date: 2026-05-12 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260512_0037"
down_revision = "20260509_0036"
branch_labels = None
depends_on = None

OLD_MODEL = "speech-2.8-hd"
NEW_MODEL = "speech-2.8-turbo"


def upgrade() -> None:
    op.alter_column(
        "characters",
        "voice_model",
        server_default=sa.text(f"'{NEW_MODEL}'"),
        existing_type=sa.String(length=120),
        existing_nullable=False,
    )
    op.execute(
        f"UPDATE characters "
        f"SET voice_model = '{NEW_MODEL}' "
        f"WHERE voice_source_type = 'system' AND voice_model = '{OLD_MODEL}'"
    )
    op.execute(
        f"UPDATE voice_profiles "
        f"SET provider_model = '{NEW_MODEL}' "
        f"WHERE source_type = 'system' AND provider_model = '{OLD_MODEL}'"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE voice_profiles "
        f"SET provider_model = '{OLD_MODEL}' "
        f"WHERE source_type = 'system' AND provider_model = '{NEW_MODEL}'"
    )
    op.execute(
        f"UPDATE characters "
        f"SET voice_model = '{OLD_MODEL}' "
        f"WHERE voice_source_type = 'system' AND voice_model = '{NEW_MODEL}'"
    )
    op.alter_column(
        "characters",
        "voice_model",
        server_default=sa.text(f"'{OLD_MODEL}'"),
        existing_type=sa.String(length=120),
        existing_nullable=False,
    )
