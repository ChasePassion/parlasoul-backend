"""Add chat titles and history sorting indexes for Phase 5."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260324_0017"
down_revision = "20260324_0016"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
ALTER TABLE chats
  ADD COLUMN IF NOT EXISTS title VARCHAR(120);

UPDATE chats
SET title = 'New Chat'
WHERE title IS NULL OR btrim(title) = '';

ALTER TABLE chats
  ALTER COLUMN title SET DEFAULT 'New Chat',
  ALTER COLUMN title SET NOT NULL;

CREATE INDEX IF NOT EXISTS chats_user_state_sort_idx
  ON chats (user_id, state, (COALESCE(last_turn_at, created_at)) DESC, id DESC);

CREATE INDEX IF NOT EXISTS chats_user_character_state_sort_idx
  ON chats (user_id, character_id, state, (COALESCE(last_turn_at, created_at)) DESC, id DESC);
"""

_DOWNGRADE_SQL = """
DROP INDEX IF EXISTS chats_user_character_state_sort_idx;
DROP INDEX IF EXISTS chats_user_state_sort_idx;

ALTER TABLE chats
  DROP COLUMN IF EXISTS title;
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
