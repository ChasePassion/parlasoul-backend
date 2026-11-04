"""Remove sentence-level IPA field from reply-card payloads."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260226_0005"
down_revision = "20260224_0004"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
UPDATE candidates
SET extra = jsonb_set(
  extra,
  '{reply_card}',
  (extra -> 'reply_card') - 'ipa_us',
  true
)
WHERE extra ? 'reply_card'
  AND jsonb_typeof(extra -> 'reply_card') = 'object'
  AND (extra -> 'reply_card') ? 'ipa_us';

UPDATE saved_items
SET card = card - 'ipa_us'
WHERE kind = 'reply_card'
  AND card ? 'ipa_us';

UPDATE saved_items
SET card = jsonb_set(
  card,
  '{reply_card}',
  (card -> 'reply_card') - 'ipa_us',
  true
)
WHERE kind = 'reply_card'
  AND card ? 'reply_card'
  AND jsonb_typeof(card -> 'reply_card') = 'object'
  AND (card -> 'reply_card') ? 'ipa_us';
"""


def _split_sql_statements(sql: str) -> list[str]:
    return [statement.strip() + ";" for statement in sql.split(";") if statement.strip()]


def upgrade() -> None:
    bind = op.get_bind()
    for statement in _split_sql_statements(_UPGRADE_SQL):
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    # Historical data cleanup is not safely reversible.
    pass
