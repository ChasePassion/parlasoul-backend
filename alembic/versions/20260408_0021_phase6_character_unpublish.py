"""Phase 6 character unpublish lifecycle."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260408_0021"
down_revision = "20260405_0020"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
ALTER TABLE characters
  ADD COLUMN IF NOT EXISTS status VARCHAR(20),
  ADD COLUMN IF NOT EXISTS unpublished_at TIMESTAMPTZ;

UPDATE characters
SET status = 'ACTIVE'
WHERE status IS NULL;

ALTER TABLE characters
  ALTER COLUMN status SET DEFAULT 'ACTIVE',
  ALTER COLUMN status SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'characters_status_check'
  ) THEN
    ALTER TABLE characters
      ADD CONSTRAINT characters_status_check
      CHECK (status IN ('ACTIVE', 'UNPUBLISHED'));
  END IF;
END $$;
"""

_DOWNGRADE_SQL = """
ALTER TABLE characters
  DROP CONSTRAINT IF EXISTS characters_status_check;

ALTER TABLE characters
  DROP COLUMN IF EXISTS unpublished_at;

ALTER TABLE characters
  DROP COLUMN IF EXISTS status;
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
