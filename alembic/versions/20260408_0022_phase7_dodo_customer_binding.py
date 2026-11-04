"""Add Dodo customer binding column to users."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260408_0022"
down_revision = "20260408_0021"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS "dodoCustomerId" TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS users_dodo_customer_id_uniq
  ON users ("dodoCustomerId")
  WHERE "dodoCustomerId" IS NOT NULL;
"""

_DOWNGRADE_SQL = """
DROP INDEX IF EXISTS users_dodo_customer_id_uniq;

ALTER TABLE users
  DROP COLUMN IF EXISTS "dodoCustomerId";
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
