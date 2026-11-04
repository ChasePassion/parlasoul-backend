
-- User settings table for chat UI preferences.
-- Includes RLS to ensure each user can only access their own settings row.

BEGIN;

CREATE TABLE IF NOT EXISTS user_settings (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  -- Default value is controlled by application settings when creating rows.
  message_font_size SMALLINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT user_settings_message_font_size_check
    CHECK (message_font_size BETWEEN 14 AND 24)
);

CREATE INDEX IF NOT EXISTS user_settings_user_id_idx ON user_settings (user_id);

ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_settings FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_settings_scope ON user_settings;
CREATE POLICY user_settings_scope ON user_settings
  FOR ALL
  USING (
    user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  )
  WITH CHECK (
    user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  );

COMMIT;
