-- Unify table ownership for LOCAL DEVELOPMENT only.
-- Production tables are owned by parlasoul_migrator and do not need this file.
--
-- Usage (local dev):
--   psql -h localhost -U postgres -d role_play_mem -f docs/database/local_fix_owners.sql

ALTER TABLE users OWNER TO role_play_user;
ALTER TABLE characters OWNER TO role_play_user;
ALTER TABLE chats OWNER TO role_play_user;
ALTER TABLE turns OWNER TO role_play_user;
ALTER TABLE candidates OWNER TO role_play_user;
ALTER TABLE user_settings OWNER TO role_play_user;
ALTER TABLE alembic_version OWNER TO role_play_user;
