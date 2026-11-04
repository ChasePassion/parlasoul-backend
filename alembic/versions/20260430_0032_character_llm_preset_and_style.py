"""add character llm_preset_id and dialogue_style_id columns

Revision ID: 20260430_0032
Revises: 20260430_0031
Create Date: 2026-04-30 00:32:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "20260430_0032"
down_revision = "20260430_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE characters
          ADD COLUMN IF NOT EXISTS llm_preset_id VARCHAR(40);
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          ADD COLUMN IF NOT EXISTS dialogue_style_id VARCHAR(40);
        """
    )
    op.execute(
        """
        UPDATE characters
        SET llm_preset_id = 'free'
        WHERE llm_preset_id IS NULL
           OR llm_preset_id NOT IN ('free', 'flagship');
        """
    )
    op.execute(
        """
        UPDATE characters
        SET dialogue_style_id = 'true_nature'
        WHERE dialogue_style_id IS NULL
           OR dialogue_style_id NOT IN (
             'clear_inquiry',
             'free_spirit',
             'poetic_reserve',
             'proud_resolve',
             'spring_breeze',
             'true_nature'
           );
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          ALTER COLUMN llm_preset_id SET DEFAULT 'free',
          ALTER COLUMN llm_preset_id SET NOT NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          ALTER COLUMN dialogue_style_id SET DEFAULT 'true_nature',
          ALTER COLUMN dialogue_style_id SET NOT NULL;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'characters_llm_preset_id_check'
          ) THEN
            ALTER TABLE characters
              ADD CONSTRAINT characters_llm_preset_id_check
              CHECK (llm_preset_id IN ('free', 'flagship'));
          END IF;

          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'characters_dialogue_style_id_check'
          ) THEN
            ALTER TABLE characters
              ADD CONSTRAINT characters_dialogue_style_id_check
              CHECK (
                dialogue_style_id IN (
                  'clear_inquiry',
                  'free_spirit',
                  'poetic_reserve',
                  'proud_resolve',
                  'spring_breeze',
                  'true_nature'
                )
            );
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE characters
          DROP CONSTRAINT IF EXISTS characters_dialogue_style_id_check;
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          DROP CONSTRAINT IF EXISTS characters_llm_preset_id_check;
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          DROP COLUMN IF EXISTS dialogue_style_id;
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          DROP COLUMN IF EXISTS llm_preset_id;
        """
    )
