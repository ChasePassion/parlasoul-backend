"""remove character legacy llm provider/model columns

Revision ID: 20260430_0033
Revises: 20260430_0032
Create Date: 2026-04-30 18:10:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "20260430_0033"
down_revision = "20260430_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE characters
          DROP CONSTRAINT IF EXISTS characters_llm_binding_pair_check;
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          DROP COLUMN IF EXISTS llm_provider;
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          DROP COLUMN IF EXISTS llm_model;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE characters
          ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(40);
        """
    )
    op.execute(
        """
        ALTER TABLE characters
          ADD COLUMN IF NOT EXISTS llm_model VARCHAR(120);
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'characters_llm_binding_pair_check'
          ) THEN
            ALTER TABLE characters
              ADD CONSTRAINT characters_llm_binding_pair_check
              CHECK (
                (llm_provider IS NULL AND llm_model IS NULL)
                OR (llm_provider IS NOT NULL AND llm_model IS NOT NULL)
              );
          END IF;
        END $$;
        """
    )
