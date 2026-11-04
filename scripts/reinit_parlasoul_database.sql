-- Rebuild the parlasoul database before restoring a dump.
-- Run as the postgres superuser:
--   psql -v ON_ERROR_STOP=1 -U postgres -d postgres < reinit_parlasoul_database.sql
--
-- Extensions must be owned by parlasoul_migrator, otherwise pg_restore fails
-- with "must be owner of extension" while restoring extension comments.

DROP DATABASE IF EXISTS parlasoul WITH (FORCE);
CREATE DATABASE parlasoul OWNER parlasoul_migrator;
ALTER DATABASE parlasoul SET timezone = 'UTC';

\connect parlasoul

CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public;

SET ROLE parlasoul_migrator;
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;
GRANT USAGE ON SCHEMA public TO parlasoul_app;
ALTER DEFAULT PRIVILEGES FOR ROLE parlasoul_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO parlasoul_app;
ALTER DEFAULT PRIVILEGES FOR ROLE parlasoul_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO parlasoul_app;
ALTER DEFAULT PRIVILEGES FOR ROLE parlasoul_migrator IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO parlasoul_app;
RESET ROLE;

GRANT CONNECT ON DATABASE parlasoul TO parlasoul_app;
