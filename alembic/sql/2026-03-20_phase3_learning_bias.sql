
-- Phase 3 learning bias
-- 1) Normalize saved_items display fields from kind-specific card payloads.
-- 2) Deduplicate by (user_id, kind, display_surface).
-- 3) Replace source_message-based unique constraint with surface-based unique constraint.

BEGIN;

UPDATE saved_items
SET
  display_surface = COALESCE(
    CASE kind
      WHEN 'sentence_card' THEN COALESCE(
        NULLIF(card ->> 'surface', ''),
        NULLIF(card #>> '{sentence_card,surface}', ''),
        display_surface
      )
      WHEN 'word_card' THEN COALESCE(
        NULLIF(card ->> 'surface', ''),
        NULLIF(card #>> '{word_card,surface}', ''),
        display_surface
      )
      WHEN 'feedback_card' THEN COALESCE(
        NULLIF(card ->> 'surface', ''),
        NULLIF(card #>> '{feedback_card,surface}', ''),
        display_surface
      )
      ELSE display_surface
    END,
    display_surface
  ),
  display_zh = COALESCE(
    CASE kind
      WHEN 'sentence_card' THEN COALESCE(
        NULLIF(card ->> 'zh', ''),
        NULLIF(card #>> '{sentence_card,zh}', ''),
        display_zh
      )
      WHEN 'word_card' THEN COALESCE(
        NULLIF(card #>> '{pos_groups,0,senses,0,zh}', ''),
        NULLIF(card #>> '{word_card,pos_groups,0,senses,0,zh}', ''),
        NULLIF(card #>> '{example,zh}', ''),
        NULLIF(card #>> '{word_card,example,zh}', ''),
        display_zh
      )
      WHEN 'feedback_card' THEN COALESCE(
        NULLIF(card ->> 'zh', ''),
        NULLIF(card #>> '{feedback_card,zh}', ''),
        display_zh
      )
      ELSE display_zh
    END,
    display_zh
  );

WITH ranked AS (
  SELECT
    id,
    ROW_NUMBER() OVER (
      PARTITION BY user_id, kind, display_surface
      ORDER BY created_at DESC, id DESC
    ) AS rn
  FROM saved_items
)
DELETE FROM saved_items s
USING ranked r
WHERE s.id = r.id
  AND r.rn > 1;

ALTER TABLE saved_items
  DROP CONSTRAINT IF EXISTS saved_items_user_kind_source_message_uniq;

ALTER TABLE saved_items
  DROP CONSTRAINT IF EXISTS saved_items_user_kind_surface_uniq;

ALTER TABLE saved_items
  ADD CONSTRAINT saved_items_user_kind_surface_uniq
  UNIQUE (user_id, kind, display_surface);

COMMIT;
