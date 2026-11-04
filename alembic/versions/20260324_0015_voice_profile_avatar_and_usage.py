"""Add voice profile avatars and backfill local voice metadata."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260324_0015"
down_revision = "20260321_0014"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
ALTER TABLE voice_profiles
  ADD COLUMN IF NOT EXISTS avatar_file_name VARCHAR(255) NULL;

UPDATE voice_profiles
SET avatar_file_name = '/default-avatar.svg'
WHERE avatar_file_name IS NULL OR btrim(avatar_file_name) = '';
"""

_DOWNGRADE_SQL = """
ALTER TABLE voice_profiles
  DROP COLUMN IF EXISTS avatar_file_name;
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
