#!/usr/bin/env bash
# Restore a db_backup.sh dump into the reference TimescaleDB Compose service
# (quant_timescaledb) — issue #86 DoD companion to db_backup.sh. Exercise this
# at least once (e.g. against a scratch container) before trusting the
# backup: an untested backup is not a disaster-recovery procedure.
#
# Usage:
#   ./deploy/db_restore.sh <dump_file>
#
# Drops and recreates the "quant" database, then restores into that fresh
# database — matches TimescaleDB's own restore guidance. pg_restore --clean
# against an existing, live hypertable fails ("ONLY option not supported on
# hypertable operations": Postgres emits ALTER TABLE ONLY ... DROP CONSTRAINT
# for the foreign keys pg_dump ordered around hypertables, which TimescaleDB
# rejects) even though the same dump replays cleanly into an empty database.
# This is destructive to whatever is currently in "quant" — confirm the
# target container on purpose, this does not prompt.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DUMP_FILE="${1:?Usage: db_restore.sh <dump_file>}"
CONTAINER="quant_timescaledb"

if [[ ! -f "${DUMP_FILE}" ]]; then
    echo "Dump file not found: ${DUMP_FILE}" >&2
    exit 1
fi

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env"
    set +a
fi
: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}"

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "${CONTAINER} is not running — start it first: cd deploy && docker compose up -d timescaledb" >&2
    exit 1
fi

echo "Recreating an empty 'quant' database on ${CONTAINER} (drops the current one)..."
# DROP/CREATE DATABASE cannot run inside a transaction block, so each
# statement needs its own -c call rather than one semicolon-joined string.
docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${CONTAINER}" psql -U quant -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'quant' AND pid <> pg_backend_pid();"
docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${CONTAINER}" psql -U quant -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS quant;"
docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${CONTAINER}" psql -U quant -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE quant OWNER quant;"
docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${CONTAINER}" \
    psql -U quant -d quant -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "Restoring ${DUMP_FILE} -> ${CONTAINER}:quant ..."
docker cp "${DUMP_FILE}" "${CONTAINER}:/tmp/restore.dump"
docker exec -e PGPASSWORD="${POSTGRES_PASSWORD}" "${CONTAINER}" \
    pg_restore -U quant -d quant --no-owner /tmp/restore.dump
docker exec "${CONTAINER}" rm /tmp/restore.dump

echo "Done. Sanity check: docker exec -it ${CONTAINER} psql -U quant -d quant -c '\\dt'"
