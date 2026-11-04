
-- Phase 1 learning features:
-- 1) Extend user_settings with display and learning toggles.
-- 2) Create saved_items favorites table.

BEGIN;

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS display_mode VARCHAR(20) NOT NULL DEFAULT 'concise',
  ADD COLUMN IF NOT EXISTS knowledge_card_enabled BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS mixed_input_auto_translate_enabled BOOLEAN NOT NULL DEFAULT true;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'user_settings_display_mode_check'
  ) THEN
    ALTER TABLE user_settings
      ADD CONSTRAINT user_settings_display_mode_check
      CHECK (display_mode IN ('concise', 'detailed'));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS saved_items (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind VARCHAR(50) NOT NULL,
  display_surface TEXT NOT NULL,
  display_zh TEXT NOT NULL,
  card JSONB NOT NULL,
  source_role_id UUID NOT NULL,
  source_chat_id UUID NOT NULL,
  source_message_id VARCHAR(255) NOT NULL,
  source_turn_id UUID NULL,
  source_candidate_id UUID NULL,
  source_meta JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT saved_items_user_kind_source_message_uniq
    UNIQUE (user_id, kind, source_message_id)
);

CREATE INDEX IF NOT EXISTS saved_items_user_created_at_idx
  ON saved_items (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS saved_items_user_kind_idx
  ON saved_items (user_id, kind);
CREATE INDEX IF NOT EXISTS saved_items_user_source_chat_idx
  ON saved_items (user_id, source_chat_id);

ALTER TABLE saved_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE saved_items FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS saved_items_scope ON saved_items;
CREATE POLICY saved_items_scope ON saved_items
  FOR ALL
  USING (
    user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  )
  WITH CHECK (
    user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  );

COMMIT;

