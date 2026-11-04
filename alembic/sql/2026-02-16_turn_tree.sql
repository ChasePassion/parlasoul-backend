
-- Turn tree migration (no Alembic).
--
-- Goal:
-- - Turns become a tree by adding parent pointers:
--     turns.parent_turn_id        -> parent turn (bubble)
--     turns.parent_candidate_id   -> which candidate version produced this child
-- - Chats track the currently active branch leaf:
--     chats.active_leaf_turn_id
-- - Chats track monotonic turn_no allocation without scanning:
--     chats.last_turn_no
--
-- Notes:
-- - This repo historically didn't enforce uniqueness for (chat_id, turn_no) or
--   (turn_id, candidate_no). This migration adds both constraints.
-- - Each parent_candidate_id can have at most one child turn. This makes a
--   selected candidate deterministically define the downstream branch.
-- - If your existing schema defines turns.turn_no / candidates.candidate_no as
--   GENERATED ALWAYS AS IDENTITY, the app cannot insert per-chat/per-turn
--   sequence numbers. This migration drops those identities and re-sequences
--   existing rows to dense 1..N numbering within their scope.

BEGIN;

-- 0) Ensure turn_no/candidate_no are application-assigned (not GENERATED ALWAYS).
-- Drop legacy global uniqueness that breaks per-chat/per-turn numbering.
ALTER TABLE turns DROP CONSTRAINT IF EXISTS turns_turn_no_key;
ALTER TABLE candidates DROP CONSTRAINT IF EXISTS candidates_candidate_no_key;

ALTER TABLE turns ALTER COLUMN turn_no DROP IDENTITY IF EXISTS;
ALTER TABLE candidates ALTER COLUMN candidate_no DROP IDENTITY IF EXISTS;

-- Re-sequence existing data to 1..N for stable UI (k/n).
WITH ordered_turns AS (
  SELECT
    id,
    chat_id,
    ROW_NUMBER() OVER (PARTITION BY chat_id ORDER BY turn_no) AS new_turn_no
  FROM turns
)
UPDATE turns t
SET turn_no = o.new_turn_no
FROM ordered_turns o
WHERE t.id = o.id
  AND t.turn_no IS DISTINCT FROM o.new_turn_no;

WITH ordered_candidates AS (
  SELECT
    id,
    turn_id,
    ROW_NUMBER() OVER (PARTITION BY turn_id ORDER BY candidate_no) AS new_candidate_no
  FROM candidates
)
UPDATE candidates c
SET candidate_no = o.new_candidate_no
FROM ordered_candidates o
WHERE c.id = o.id
  AND c.candidate_no IS DISTINCT FROM o.new_candidate_no;

-- 1) Columns
ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_turn_no BIGINT NULL;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS active_leaf_turn_id UUID NULL;

ALTER TABLE turns ADD COLUMN IF NOT EXISTS parent_turn_id UUID NULL;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS parent_candidate_id UUID NULL;

-- 2) Constraints / Indexes
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'turns_chat_turn_no_uniq'
  ) THEN
    ALTER TABLE turns
      ADD CONSTRAINT turns_chat_turn_no_uniq UNIQUE (chat_id, turn_no);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'candidates_turn_candidate_no_uniq'
  ) THEN
    ALTER TABLE candidates
      ADD CONSTRAINT candidates_turn_candidate_no_uniq UNIQUE (turn_id, candidate_no);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS turns_chat_parent_turn_idx
  ON turns (chat_id, parent_turn_id);

-- One candidate version can only lead to one child turn (deterministic branch).
CREATE UNIQUE INDEX IF NOT EXISTS turns_parent_candidate_uniq
  ON turns (parent_candidate_id)
  WHERE parent_candidate_id IS NOT NULL;

-- 3) Backfill

-- chats.last_turn_no = max(turn_no) per chat
UPDATE chats c
SET last_turn_no = t.max_turn_no
FROM (
  SELECT chat_id, MAX(turn_no) AS max_turn_no
  FROM turns
  GROUP BY chat_id
) t
WHERE c.id = t.chat_id
  AND (c.last_turn_no IS NULL OR c.last_turn_no <> t.max_turn_no);

-- chats.active_leaf_turn_id: prefer last_turn_id, otherwise use max(turn_no) turn id
UPDATE chats c
SET active_leaf_turn_id = c.last_turn_id
WHERE c.active_leaf_turn_id IS NULL
  AND c.last_turn_id IS NOT NULL;

UPDATE chats c
SET active_leaf_turn_id = x.last_turn_id
FROM (
  SELECT DISTINCT ON (chat_id) chat_id, id AS last_turn_id
  FROM turns
  ORDER BY chat_id, turn_no DESC
) x
WHERE c.id = x.chat_id
  AND c.active_leaf_turn_id IS NULL;

-- Backfill linear chain into "main branch": each turn points to previous turn + its primary candidate.
WITH ordered AS (
  SELECT
    t.id AS turn_id,
    t.chat_id,
    t.turn_no,
    LAG(t.id) OVER (PARTITION BY t.chat_id ORDER BY t.turn_no) AS parent_turn_id,
    LAG(t.primary_candidate_id) OVER (PARTITION BY t.chat_id ORDER BY t.turn_no) AS parent_candidate_id
  FROM turns t
)
UPDATE turns t
SET parent_turn_id = o.parent_turn_id,
    parent_candidate_id = o.parent_candidate_id
FROM ordered o
WHERE t.id = o.turn_id
  AND (t.parent_turn_id IS DISTINCT FROM o.parent_turn_id
       OR t.parent_candidate_id IS DISTINCT FROM o.parent_candidate_id);

COMMIT;
