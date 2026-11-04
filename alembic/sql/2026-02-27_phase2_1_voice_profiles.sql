
-- Phase 2.1 voice profiles:
-- 1) Add provider-agnostic voice_profiles table.
-- 2) Add characters.voice_profile_id and backfill existing rows.

BEGIN;

CREATE TABLE IF NOT EXISTS voice_profiles (
  id UUID PRIMARY KEY,
  owner_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  provider VARCHAR(40) NOT NULL,
  provider_voice_id VARCHAR(191) NOT NULL,
  source_type VARCHAR(20) NOT NULL,
  status VARCHAR(20) NOT NULL,
  provider_status VARCHAR(40) NULL,
  provider_model VARCHAR(120) NULL,
  display_name VARCHAR(80) NOT NULL,
  description TEXT NULL,
  preview_audio_url TEXT NULL,
  language_tags TEXT[] NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'voice_profiles_provider_voice_uniq'
  ) THEN
    ALTER TABLE voice_profiles
      ADD CONSTRAINT voice_profiles_provider_voice_uniq
      UNIQUE (provider, provider_voice_id);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'voice_profiles_source_type_check'
  ) THEN
    ALTER TABLE voice_profiles
      ADD CONSTRAINT voice_profiles_source_type_check
      CHECK (source_type IN ('system', 'clone', 'designed', 'imported'));
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'voice_profiles_status_check'
  ) THEN
    ALTER TABLE voice_profiles
      ADD CONSTRAINT voice_profiles_status_check
      CHECK (status IN ('creating', 'processing', 'ready', 'failed', 'deleting', 'deleted'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS voice_profiles_owner_user_created_at_idx
  ON voice_profiles (owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS voice_profiles_provider_status_idx
  ON voice_profiles (provider, status);

-- Seed one default system voice for historical character backfill.
INSERT INTO voice_profiles (
  id,
  owner_user_id,
  provider,
  provider_voice_id,
  source_type,
  status,
  provider_status,
  provider_model,
  display_name,
  description,
  preview_audio_url,
  language_tags,
  metadata
)
VALUES (
  '7f9fba53-c44e-4f8f-9457-85cf7c4f8a12'::uuid,
  NULL,
  'dashscope',
  'Cherry',
  'system',
  'ready',
  'active',
  'qwen3-tts-instruct-flash-realtime',
  'Cherry',
  'Default system voice',
  NULL,
  ARRAY['zh-CN', 'en-US'],
  '{}'::jsonb
)
ON CONFLICT (provider, provider_voice_id) DO NOTHING;

ALTER TABLE characters
  ADD COLUMN IF NOT EXISTS voice_profile_id UUID NULL;

WITH default_voice AS (
  SELECT id
  FROM voice_profiles
  WHERE owner_user_id IS NULL
    AND source_type = 'system'
    AND status = 'ready'
  ORDER BY created_at ASC
  LIMIT 1
)
UPDATE characters AS c
SET voice_profile_id = dv.id
FROM default_voice AS dv
WHERE c.voice_profile_id IS NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'characters_voice_profile_id_fkey'
  ) THEN
    ALTER TABLE characters
      ADD CONSTRAINT characters_voice_profile_id_fkey
      FOREIGN KEY (voice_profile_id)
      REFERENCES voice_profiles(id)
      ON DELETE RESTRICT;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS characters_voice_profile_id_idx
  ON characters (voice_profile_id);

ALTER TABLE characters
  ALTER COLUMN voice_profile_id SET NOT NULL;

COMMIT;
