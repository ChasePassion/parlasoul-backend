"""Add Phase 5 growth system tables."""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260404_0019"
down_revision = "20260326_0018"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS growth_user_stats (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  current_natural_streak BIGINT NOT NULL DEFAULT 0,
  longest_natural_streak BIGINT NOT NULL DEFAULT 0,
  makeup_card_balance BIGINT NOT NULL DEFAULT 0,
  last_natural_signin_date DATE NULL,
  last_rewarded_natural_streak BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS growth_daily_stats (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  stat_date DATE NOT NULL,
  user_signin_message_count BIGINT NOT NULL DEFAULT 0,
  total_message_count BIGINT NOT NULL DEFAULT 0,
  total_word_count BIGINT NOT NULL DEFAULT 0,
  is_natural_signed BOOLEAN NOT NULL DEFAULT FALSE,
  natural_signed_at TIMESTAMPTZ NULL,
  is_makeup_signed BOOLEAN NOT NULL DEFAULT FALSE,
  makeup_signed_at TIMESTAMPTZ NULL,
  popup_consumed_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT growth_daily_stats_user_date_uniq UNIQUE (user_id, stat_date)
);

CREATE TABLE IF NOT EXISTS growth_character_daily_stats (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  character_id UUID NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
  stat_date DATE NOT NULL,
  total_message_count BIGINT NOT NULL DEFAULT 0,
  total_word_count BIGINT NOT NULL DEFAULT 0,
  total_exchange_count BIGINT NOT NULL DEFAULT 0,
  last_chat_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT growth_character_daily_stats_user_character_date_uniq UNIQUE (user_id, character_id, stat_date)
);

CREATE TABLE IF NOT EXISTS growth_character_stats (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  character_id UUID NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
  total_message_count BIGINT NOT NULL DEFAULT 0,
  total_word_count BIGINT NOT NULL DEFAULT 0,
  total_exchange_count BIGINT NOT NULL DEFAULT 0,
  chatted_days_count BIGINT NOT NULL DEFAULT 0,
  last_chat_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT growth_character_stats_user_character_uniq UNIQUE (user_id, character_id)
);

CREATE TABLE IF NOT EXISTS growth_share_triggers (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  chat_id UUID NULL,
  character_id UUID NULL,
  trigger_kind VARCHAR(50) NOT NULL,
  trigger_key VARCHAR(255) NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  consumed_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT growth_share_triggers_kind_check
    CHECK (trigger_kind IN ('daily_signin_completed', 'character_message_milestone')),
  CONSTRAINT growth_share_triggers_trigger_key_uniq UNIQUE (trigger_key)
);

CREATE INDEX IF NOT EXISTS growth_daily_stats_user_date_idx
  ON growth_daily_stats (user_id, stat_date DESC);

CREATE INDEX IF NOT EXISTS growth_character_daily_stats_user_date_idx
  ON growth_character_daily_stats (user_id, stat_date DESC);

CREATE INDEX IF NOT EXISTS growth_character_daily_stats_user_character_date_idx
  ON growth_character_daily_stats (user_id, character_id, stat_date DESC);

CREATE INDEX IF NOT EXISTS growth_character_stats_user_sort_messages_idx
  ON growth_character_stats (user_id, total_message_count DESC, chatted_days_count DESC, total_exchange_count DESC, character_id DESC);

CREATE INDEX IF NOT EXISTS growth_character_stats_user_sort_words_idx
  ON growth_character_stats (user_id, total_word_count DESC, chatted_days_count DESC, total_exchange_count DESC, character_id DESC);

CREATE INDEX IF NOT EXISTS growth_character_stats_user_sort_days_idx
  ON growth_character_stats (user_id, chatted_days_count DESC, total_message_count DESC, total_exchange_count DESC, character_id DESC);

CREATE INDEX IF NOT EXISTS growth_share_triggers_user_pending_idx
  ON growth_share_triggers (user_id, consumed_at, triggered_at, id);
"""

_DOWNGRADE_SQL = """
DROP INDEX IF EXISTS growth_share_triggers_user_pending_idx;
DROP INDEX IF EXISTS growth_character_stats_user_sort_days_idx;
DROP INDEX IF EXISTS growth_character_stats_user_sort_words_idx;
DROP INDEX IF EXISTS growth_character_stats_user_sort_messages_idx;
DROP INDEX IF EXISTS growth_character_daily_stats_user_character_date_idx;
DROP INDEX IF EXISTS growth_character_daily_stats_user_date_idx;
DROP INDEX IF EXISTS growth_daily_stats_user_date_idx;

DROP TABLE IF EXISTS growth_share_triggers;
DROP TABLE IF EXISTS growth_character_stats;
DROP TABLE IF EXISTS growth_character_daily_stats;
DROP TABLE IF EXISTS growth_daily_stats;
DROP TABLE IF EXISTS growth_user_stats;
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
