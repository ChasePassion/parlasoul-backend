"""Rename user settings reply-card toggle column."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260321_0014"
down_revision = "20260321_0013"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = ANY (current_schemas(true))
      AND table_name = 'user_settings'
      AND column_name = 'knowledge_card_enabled'
  ) AND NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = ANY (current_schemas(true))
      AND table_name = 'user_settings'
      AND column_name = 'reply_card_enabled'
  ) THEN
    ALTER TABLE user_settings
      RENAME COLUMN knowledge_card_enabled TO reply_card_enabled;
  END IF;
END $$;
"""

_DOWNGRADE_SQL = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = ANY (current_schemas(true))
      AND table_name = 'user_settings'
      AND column_name = 'reply_card_enabled'
  ) AND NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = ANY (current_schemas(true))
      AND table_name = 'user_settings'
      AND column_name = 'knowledge_card_enabled'
  ) THEN
    ALTER TABLE user_settings
      RENAME COLUMN reply_card_enabled TO knowledge_card_enabled;
  END IF;
END $$;
"""


def _split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    in_dollar_block = False

    for line in sql.splitlines():
        buffer.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar_block = not in_dollar_block

        if not in_dollar_block and line.rstrip().endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()

    tail = "\n".join(buffer).strip()
    if tail:
        statements.append(tail)

    return statements


def upgrade() -> None:
    bind = op.get_bind()
    for statement in _split_sql_statements(_UPGRADE_SQL):
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    for statement in _split_sql_statements(_DOWNGRADE_SQL):
        bind.exec_driver_sql(statement)
