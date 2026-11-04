#!/usr/bin/env bash
set -euo pipefail

ROOT="${PARLASOUL_ROOT:-/srv/parlasoul}"
BACKUP_DIR="${POSTGRES_BACKUP_DIR:-$ROOT/backups/postgres}"
RETENTION_DAYS="${POSTGRES_BACKUP_RETENTION_DAYS:-7}"
CONTAINER="${POSTGRES_CONTAINER:-parlasoul-postgres-1}"
DB="${POSTGRES_DB_NAME:-parlasoul}"
ROLE="${POSTGRES_DUMP_ROLE:-parlasoul_migrator}"

mkdir -p "$BACKUP_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
filename="parlasoul-${timestamp}.dump"

docker exec "$CONTAINER" pg_dump \
  --username "$ROLE" \
  --dbname "$DB" \
  --format=custom \
  --compress=9 \
  --file="/tmp/${filename}"

docker cp "${CONTAINER}:/tmp/${filename}" "${BACKUP_DIR}/${filename}"
docker exec "$CONTAINER" rm -f "/tmp/${filename}"

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'parlasoul-*.dump' \
  -mtime "+${RETENTION_DAYS}" -delete

echo "postgres backup written: ${BACKUP_DIR}/${filename}"
