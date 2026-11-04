"""Add user_settings table and RLS policy."""

from __future__ import annotations

from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260219_0003"
down_revision = "20260218_0002"
branch_labels = None
depends_on = None


def _read_sql_file(name: str) -> str:
    root = Path(__file__).resolve().parents[2]
    raw = (root / "alembic" / "sql" / name).read_text(encoding="utf-8")
    lines: list[str] = []
    for line in raw.splitlines():
        if line.strip().upper() in {"BEGIN;", "COMMIT;"}:
            continue
        lines.append(line)
    return "\n".join(lines)


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
    sql = _read_sql_file("2026-02-19_user_settings.sql")
    bind = op.get_bind()
    for statement in _split_sql_statements(sql):
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    # Historical SQL migration is not safely reversible.
    pass
