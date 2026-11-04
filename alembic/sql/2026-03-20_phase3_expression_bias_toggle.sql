
-- Phase 3 expression bias toggle
-- Add a per-user switch to enable/disable favorite expression bias injection.

BEGIN;

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS preferred_expression_bias_enabled BOOLEAN NOT NULL DEFAULT true;

COMMIT;
