"""Switch 萌宝 (Bella) system voice from Charming_Lady to Chinese_Mandarin_Warm_Girl.

Revision ID: 20260509_0036
Revises: 20260509_0035
Create Date: 2026-05-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "20260509_0036"
down_revision = "20260509_0035"
branch_labels = None
depends_on = None

OLD_VOICE_ID = "Charming_Lady"
NEW_VOICE_ID = "Chinese_Mandarin_Warm_Girl"


def upgrade() -> None:
    op.execute(
        f"UPDATE characters "
        f"SET voice_provider_voice_id = '{NEW_VOICE_ID}' "
        f"WHERE voice_source_type = 'system' "
        f"AND voice_provider = 'minimax' "
        f"AND voice_provider_voice_id = '{OLD_VOICE_ID}'"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE characters "
        f"SET voice_provider_voice_id = '{OLD_VOICE_ID}' "
        f"WHERE voice_source_type = 'system' "
        f"AND voice_provider = 'minimax' "
        f"AND voice_provider_voice_id = '{NEW_VOICE_ID}'"
    )
