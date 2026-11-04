
-- Row-level security baseline for single-DB multi-user isolation.
--
-- This migration assumes the application sets:
--   set_config('app.current_user_id', '<uuid>', false)
-- on authenticated requests.

BEGIN;

-- Supporting indexes for owner checks and policy joins.
CREATE INDEX IF NOT EXISTS characters_creator_visibility_idx
  ON characters (creator_id, visibility);
CREATE INDEX IF NOT EXISTS chats_user_id_idx
  ON chats (user_id);
CREATE INDEX IF NOT EXISTS turns_chat_id_idx
  ON turns (chat_id);
CREATE INDEX IF NOT EXISTS candidates_turn_id_idx
  ON candidates (turn_id);

-- Enable and enforce RLS.
ALTER TABLE characters ENABLE ROW LEVEL SECURITY;
ALTER TABLE chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;

ALTER TABLE characters FORCE ROW LEVEL SECURITY;
ALTER TABLE chats FORCE ROW LEVEL SECURITY;
ALTER TABLE turns FORCE ROW LEVEL SECURITY;
ALTER TABLE candidates FORCE ROW LEVEL SECURITY;

-- Clean up old policies so migration is re-runnable.
DROP POLICY IF EXISTS characters_select_scope ON characters;
DROP POLICY IF EXISTS characters_modify_scope ON characters;
DROP POLICY IF EXISTS chats_scope ON chats;
DROP POLICY IF EXISTS turns_scope ON turns;
DROP POLICY IF EXISTS candidates_scope ON candidates;

-- Characters:
-- - PUBLIC and PRIVATE are readable by everyone.
-- - Market listing still filters to PUBLIC at the repository layer.
-- - Mutations are creator-only.
CREATE POLICY characters_select_scope ON characters
  FOR SELECT
  USING (
    visibility IN ('PUBLIC', 'PRIVATE')
    OR creator_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  );

CREATE POLICY characters_modify_scope ON characters
  FOR ALL
  USING (
    creator_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  )
  WITH CHECK (
    creator_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  );

-- Chats are private per owner.
CREATE POLICY chats_scope ON chats
  FOR ALL
  USING (
    user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  )
  WITH CHECK (
    user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
  );

-- Turns are accessible only through owner chats.
CREATE POLICY turns_scope ON turns
  FOR ALL
  USING (
    EXISTS (
      SELECT 1
      FROM chats c
      WHERE c.id = turns.chat_id
        AND c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1
      FROM chats c
      WHERE c.id = turns.chat_id
        AND c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
    )
  );

-- Candidates inherit ownership from turns -> chats.
CREATE POLICY candidates_scope ON candidates
  FOR ALL
  USING (
    EXISTS (
      SELECT 1
      FROM turns t
      JOIN chats c ON c.id = t.chat_id
      WHERE t.id = candidates.turn_id
        AND c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
    )
  )
  WITH CHECK (
    EXISTS (
      SELECT 1
      FROM turns t
      JOIN chats c ON c.id = t.chat_id
      WHERE t.id = candidates.turn_id
        AND c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
    )
  );

COMMIT;
