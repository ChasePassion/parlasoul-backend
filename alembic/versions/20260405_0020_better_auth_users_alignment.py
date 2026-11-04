"""Align users table for Better Auth single-source migration."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260405_0020"
down_revision = "20260404_0019"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS email_verified BOOLEAN;

UPDATE users
SET display_name = COALESCE(
      NULLIF(display_name, ''),
      NULLIF(username, ''),
      split_part(email, '@', 1)
    )
WHERE display_name IS NULL OR btrim(display_name) = '';

UPDATE users
SET email_verified = TRUE
WHERE email_verified IS NULL;

ALTER TABLE users
  ALTER COLUMN email_verified SET DEFAULT FALSE,
  ALTER COLUMN email_verified SET NOT NULL;
"""

_DOWNGRADE_SQL = """
ALTER TABLE users
  DROP COLUMN IF EXISTS email_verified;

ALTER TABLE users
  DROP COLUMN IF EXISTS display_name;
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
