"""Phase 4 character llm binding."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260321_0012"
down_revision = "20260320_0011"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
ALTER TABLE characters
  ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(40),
  ADD COLUMN IF NOT EXISTS llm_model VARCHAR(120);

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
        OR
        (llm_provider IS NOT NULL AND llm_model IS NOT NULL)
      );
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
    op.drop_constraint("characters_llm_binding_pair_check", "characters", type_="check")
    op.drop_column("characters", "llm_model")
    op.drop_column("characters", "llm_provider")
