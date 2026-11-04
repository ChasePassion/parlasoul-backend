"""Enforce non-null provider_model for local voice profiles."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260324_0016"
down_revision = "20260324_0015"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM voice_profiles
    WHERE provider_model IS NULL OR btrim(provider_model) = ''
  ) THEN
    RAISE EXCEPTION
      'voice_profiles.provider_model contains NULL/empty values; fix the data before applying migration 20260324_0016';
  END IF;
END $$;

ALTER TABLE voice_profiles
  ALTER COLUMN provider_model SET NOT NULL;
"""

_DOWNGRADE_SQL = """
ALTER TABLE voice_profiles
  ALTER COLUMN provider_model DROP NOT NULL;
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
