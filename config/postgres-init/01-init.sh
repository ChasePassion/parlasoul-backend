#!/usr/bin/env bash
set -euo pipefail

: "${PARLASOUL_APP_PASSWORD:?PARLASOUL_APP_PASSWORD is required}"
: "${PARLASOUL_MIGRATOR_PASSWORD:?PARLASOUL_MIGRATOR_PASSWORD is required}"

# Two roles are required here because row level security only applies to
# roles without BYPASSRLS. Migrations and restores run as the migrator role,
# while the application must run as the app role or RLS silently stops
# filtering rows.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$PARLASOUL_APP_PASSWORD" \
  --set=migrator_password="$PARLASOUL_MIGRATOR_PASSWORD" <<'EOSQL'
CREATE ROLE parlasoul_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS
  PASSWORD :'migrator_password';
CREATE ROLE parlasoul_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
  PASSWORD :'app_password';
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=dbname=parlasoul <<'EOSQL'
SELECT format('CREATE DATABASE %I OWNER parlasoul_migrator', :'dbname')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname')\gexec
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname parlasoul <<'EOSQL'
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public;

ALTER DATABASE parlasoul SET timezone = 'UTC';
GRANT CONNECT ON DATABASE parlasoul TO parlasoul_app;
GRANT USAGE ON SCHEMA public TO parlasoul_app;

ALTER DEFAULT PRIVILEGES FOR ROLE parlasoul_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO parlasoul_app;
ALTER DEFAULT PRIVILEGES FOR ROLE parlasoul_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO parlasoul_app;
ALTER DEFAULT PRIVILEGES FOR ROLE parlasoul_migrator IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO parlasoul_app;
EOSQL
