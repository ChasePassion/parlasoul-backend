
-- Phase 2 voice features:
-- 1) Add auto-read-aloud setting toggle for user settings.

BEGIN;

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS auto_read_aloud_enabled BOOLEAN NOT NULL DEFAULT true;

COMMIT;

