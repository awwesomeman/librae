#!/usr/bin/env bash
# Back up the reference TimescaleDB Compose service (quant_timescaledb) with
# pg_dump's custom format — issue #86 DoD: "TimescaleDB backup and restore is
# documented and exercised at least once against the reference deploy/
# Compose setup." Pairs with db_restore.sh.
#
# Usage:
#   ./deploy/db_backup.sh [output_dir]   # default: ./backups (gitignored)
#
# Runs pg_dump inside the container as the quant superuser (set via
# POSTGRES_PASSWORD in .env.secrets) so the dump captures every role's
# objects, not just quant_app's. Custom format (-Fc) is what db_restore.sh
# expects.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${1:-${PROJECT_ROOT}/backups}"
CONTAINER="quant_timescaledb"

if [[ -f "${PROJECT_ROOT}/.env.secrets" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env.secrets"
    set +a
fi
: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env.secrets}"

# WHY PGPASSWORD is exported once here rather than spelled into each
# `docker exec`: `-e NAME=value` puts the value in /proc/<pid>/cmdline, which
# every local user can read, and this is the password of quant -- the role that
# owns every table. `-e NAME` with no value makes the client copy it from this
# script's environment instead, which only the same user and root can read.
# Exporting it costs nothing extra: the sourced secrets file above already put
# POSTGRES_PASSWORD in this environment with `set -a`.
export PGPASSWORD="${POSTGRES_PASSWORD}"

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
    echo "${CONTAINER} is not running — start it first: cd deploy && docker compose up -d timescaledb" >&2
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_path="${OUTPUT_DIR}/quant_${timestamp}.dump"

echo "Backing up ${CONTAINER}:quant -> ${dump_path} ..."
docker exec -e PGPASSWORD "${CONTAINER}" \
    pg_dump -U quant -d quant -Fc -f "/tmp/quant_${timestamp}.dump"
docker cp "${CONTAINER}:/tmp/quant_${timestamp}.dump" "${dump_path}"
docker exec "${CONTAINER}" rm "/tmp/quant_${timestamp}.dump"

echo "Done: ${dump_path} ($(du -h "${dump_path}" | cut -f1))"
echo "Restore with: ./deploy/db_restore.sh ${dump_path}"
