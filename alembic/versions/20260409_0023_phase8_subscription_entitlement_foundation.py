from __future__ import annotations

from alembic import op

revision = "20260409_0023"
down_revision = "20260408_0022"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS subscription_tier VARCHAR(20) DEFAULT 'free';

UPDATE users
SET subscription_tier = 'free'
WHERE subscription_tier IS NULL;

ALTER TABLE users
  ALTER COLUMN subscription_tier SET DEFAULT 'free',
  ALTER COLUMN subscription_tier SET NOT NULL;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(40);

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS subscription_product_id TEXT;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS subscription_current_period_end TIMESTAMPTZ;

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN DEFAULT TRUE;

UPDATE user_settings
SET memory_enabled = TRUE
WHERE memory_enabled IS NULL;

ALTER TABLE user_settings
  ALTER COLUMN memory_enabled SET DEFAULT TRUE,
  ALTER COLUMN memory_enabled SET NOT NULL;

CREATE TABLE IF NOT EXISTS subscription_webhook_events (
  id UUID PRIMARY KEY,
  webhook_id TEXT NOT NULL,
  event_type VARCHAR(80) NOT NULL,
  webhook_timestamp TIMESTAMPTZ NULL,
  customer_id TEXT NULL,
  subscription_id TEXT NULL,
  product_id TEXT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  processing_status VARCHAR(32) NOT NULL DEFAULT 'received',
  processed_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS subscription_webhook_events_webhook_id_uniq
  ON subscription_webhook_events (webhook_id);

CREATE INDEX IF NOT EXISTS subscription_webhook_events_customer_created_at_idx
  ON subscription_webhook_events (customer_id, created_at);

CREATE INDEX IF NOT EXISTS subscription_webhook_events_processing_status_created_at_idx
  ON subscription_webhook_events (processing_status, created_at);
"""

_DOWNGRADE_SQL = """
DROP INDEX IF EXISTS subscription_webhook_events_processing_status_created_at_idx;
DROP INDEX IF EXISTS subscription_webhook_events_customer_created_at_idx;
DROP INDEX IF EXISTS subscription_webhook_events_webhook_id_uniq;

DROP TABLE IF EXISTS subscription_webhook_events;

ALTER TABLE user_settings
  DROP COLUMN IF EXISTS memory_enabled;

ALTER TABLE users
  DROP COLUMN IF EXISTS subscription_current_period_end;

ALTER TABLE users
  DROP COLUMN IF EXISTS subscription_product_id;

ALTER TABLE users
  DROP COLUMN IF EXISTS subscription_status;

ALTER TABLE users
  DROP COLUMN IF EXISTS subscription_tier;
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
