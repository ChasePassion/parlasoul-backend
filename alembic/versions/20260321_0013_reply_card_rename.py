"""Rename legacy reply-card payload/data contracts."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260321_0013"
down_revision = "20260321_0012"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
UPDATE candidates
SET extra = (extra - 'sentence_card') || jsonb_build_object(
    'reply_card',
    COALESCE(extra -> 'reply_card', extra -> 'sentence_card')
)
WHERE extra ? 'sentence_card';

UPDATE saved_items
SET kind = 'reply_card'
WHERE kind = 'sentence_card';

UPDATE saved_items
SET card = (card - 'sentence_card') || jsonb_build_object(
    'reply_card',
    COALESCE(card -> 'reply_card', card -> 'sentence_card')
)
WHERE card ? 'sentence_card';
"""


def _split_sql_statements(sql: str) -> list[str]:
    return [statement.strip() + ";" for statement in sql.split(";") if statement.strip()]


def upgrade() -> None:
    bind = op.get_bind()
    for statement in _split_sql_statements(_UPGRADE_SQL):
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    # Historical data rename is not safely reversible.
    pass
