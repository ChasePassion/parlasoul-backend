from __future__ import annotations

from alembic import op

revision = "20260410_0024"
down_revision = "20260409_0023"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
DROP TABLE IF EXISTS email_login_codes;
"""

_DOWNGRADE_SQL = """
CREATE TABLE IF NOT EXISTS email_login_codes (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email VARCHAR(255) NOT NULL,
  code_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_email_login_codes_email_created
  ON email_login_codes (email, created_at DESC);
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
        _ = bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    for statement in _split_sql_statements(_DOWNGRADE_SQL):
        _ = bind.exec_driver_sql(statement)
