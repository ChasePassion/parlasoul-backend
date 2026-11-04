
-- Phase 2.2 character voice binding refactor:
-- 1) Remove characters.voice_profile_id FK dependency.
-- 2) Bind character voice directly by provider/model/provider_voice_id/source_type.
-- 3) Stop using local DB as source-of-truth for system voices.

BEGIN;

ALTER TABLE characters
  DROP CONSTRAINT IF EXISTS characters_voice_profile_id_fkey;

DROP INDEX IF EXISTS characters_voice_profile_id_idx;

ALTER TABLE characters
  DROP COLUMN IF EXISTS voice_profile_id;

ALTER TABLE characters
  ADD COLUMN IF NOT EXISTS voice_provider VARCHAR(40),
  ADD COLUMN IF NOT EXISTS voice_model VARCHAR(120),
  ADD COLUMN IF NOT EXISTS voice_provider_voice_id VARCHAR(191),
  ADD COLUMN IF NOT EXISTS voice_source_type VARCHAR(20);

UPDATE characters
SET
  voice_provider = COALESCE(voice_provider, 'dashscope'),
  voice_model = COALESCE(voice_model, 'qwen3-tts-instruct-flash-realtime'),
  voice_provider_voice_id = COALESCE(voice_provider_voice_id, 'Cherry'),
  voice_source_type = COALESCE(voice_source_type, 'system');

ALTER TABLE characters
  ALTER COLUMN voice_provider SET NOT NULL,
  ALTER COLUMN voice_model SET NOT NULL,
  ALTER COLUMN voice_provider_voice_id SET NOT NULL,
  ALTER COLUMN voice_source_type SET NOT NULL;

ALTER TABLE characters
  ALTER COLUMN voice_provider SET DEFAULT 'dashscope',
  ALTER COLUMN voice_model SET DEFAULT 'qwen3-tts-instruct-flash-realtime',
  ALTER COLUMN voice_provider_voice_id SET DEFAULT 'Cherry',
  ALTER COLUMN voice_source_type SET DEFAULT 'system';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'characters_voice_source_type_check'
  ) THEN
    ALTER TABLE characters
      ADD CONSTRAINT characters_voice_source_type_check
      CHECK (voice_source_type IN ('system', 'clone', 'designed', 'imported'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS characters_voice_binding_idx
  ON characters (voice_provider, voice_model, voice_provider_voice_id);

-- System voices are now sourced from DashScope directly, not local table.
DELETE FROM voice_profiles
WHERE owner_user_id IS NULL
  AND source_type = 'system';

COMMIT;
