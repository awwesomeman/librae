#!/usr/bin/env bash
# Deploy TimescaleDB to a remote host by syncing the small file subset
# docker-compose.yml actually needs (deploy/ + the schema), then running that
# same file remotely. This script does not redefine the
# containers; it only controls how the required files reach the host.
#
# Deliberately syncs only .env and the empty credential template, never account
# credential files or .env.secrets. Copy .env.secrets.example to
# .env.secrets and .credentials/<account>.env directly on the remote host,
# so a re-run cannot clobber a live key and the key never has to exist on
# the development machine.
#
# Usage: ./deploy/cloud_deploy.sh <user>@<host>
# Requires locally: rsync, ssh.
# Requires on the remote host: Bash, rsync, Docker + docker-compose-plugin,
# e.g. `apt install -y bash rsync docker.io docker-compose-plugin` — no repo.
#
# Disk sizing is on the operator: named volumes land on Docker's data-root
# (default /var/lib/docker, i.e. the root disk). Multi-disk host? Repoint
# both dockerd (/etc/docker/daemon.json "data-root") and, if containerd runs
# standalone, /etc/containerd/config.toml "root" — before running this —
# or the DB fills the root disk and crashes regardless of other disks' space.
set -Eeuo pipefail

TARGET="${1:?Usage: cloud_deploy.sh <user>@<host>}"
REMOTE_DIR="quant-deploy"
DEPLOY_TIMEOUT_SECONDS="${CLOUD_DEPLOY_TIMEOUT_SECONDS:-180}"
STAGE="initialization"

report_failure() {
    local status=$?
    echo "Cloud deployment failed during ${STAGE} (exit ${status})." >&2
    exit "${status}"
}

trap report_failure ERR

if [[ ! "${DEPLOY_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ ]]; then
    echo "CLOUD_DEPLOY_TIMEOUT_SECONDS must be a positive integer." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load deployment settings from the project root.
if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
    echo "Missing ${PROJECT_ROOT}/.env; copy .env.example and configure it first." >&2
    exit 1
fi
if [[ ! -f "${PROJECT_ROOT}/.env.secrets" ]]; then
    echo "Missing ${PROJECT_ROOT}/.env.secrets; copy .env.secrets.example and fill in the Docker Compose infra secrets section." >&2
    exit 1
fi
set -a
# shellcheck source=/dev/null
source "${PROJECT_ROOT}/.env"
# shellcheck source=/dev/null
source "${PROJECT_ROOT}/.env.secrets"
set +a
: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env.secrets}"
: "${POSTGRES_APP_PASSWORD:?Set POSTGRES_APP_PASSWORD in .env.secrets}"
: "${POSTGRES_GRAFANA_PASSWORD:?Set POSTGRES_GRAFANA_PASSWORD in .env.secrets}"

# .env is scp'd wholesale below, so a credential parked here reaches every
# host that ever syncs. The templates already document the split; this is the
# only check that reads the file that actually travels. Key names only —
# printing the value would put the secret in the terminal and CI log too.
STAGE="credential preflight"
leaked_keys="$(
    sed -E 's/^[[:space:]]*export[[:space:]]+//' "${PROJECT_ROOT}/.env" \
        | grep -E '^[A-Za-z_][A-Za-z0-9_]*(PASSWORD|SECRET|TOKEN|DSN|KEY|PERSON_ID)=.' \
        | cut -d= -f1 || true
)"
if [[ -n "${leaked_keys}" ]]; then
    echo "Refusing to sync ${PROJECT_ROOT}/.env — it assigns secrets that belong in .env.secrets:" >&2
    echo "${leaked_keys}" | sed 's/^/  /' >&2
    echo "Move them to .env.secrets (never synced), then re-run. See .env.example's header." >&2
    exit 1
fi

STAGE="file transfer"
echo "[1/4] Syncing deployment files to ${TARGET}:~/${REMOTE_DIR}/ (not the whole repo)..."
ssh "${TARGET}" "mkdir -p ${REMOTE_DIR}/deploy ${REMOTE_DIR}/librae/db"
rsync -az "${SCRIPT_DIR}/" "${TARGET}:${REMOTE_DIR}/deploy/"
rsync -az "${PROJECT_ROOT}/librae/db/timescale_init.sql" "${TARGET}:${REMOTE_DIR}/librae/db/timescale_init.sql"
scp -q \
    "${PROJECT_ROOT}/.env" \
    "${PROJECT_ROOT}/.env.secrets.example" \
    "${TARGET}:${REMOTE_DIR}/"

STAGE="Compose startup"
echo "[2/4] Starting timescaledb (same docker-compose.yml as VPS-native deploy)..."
if ! ssh "${TARGET}" "test -f ${REMOTE_DIR}/.env.secrets"; then
    echo "Missing ${REMOTE_DIR}/.env.secrets on ${TARGET}; cloud_deploy.sh never syncs it — scp .env.secrets.example there as .env.secrets and fill in the Docker Compose infra secrets section." >&2
    exit 1
fi
# --wait returns once the container's healthcheck passes, so whatever runs
# next sees a healthy service rather than one still starting.
ssh "${TARGET}" "cd ${REMOTE_DIR}/deploy && docker compose --env-file ../.env --env-file ../.env.secrets up -d --wait --wait-timeout ${DEPLOY_TIMEOUT_SECONDS} timescaledb"

STAGE="TimescaleDB readiness"
echo "[3/4] Waiting for TimescaleDB..."
deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
until ssh "${TARGET}" \
    "docker exec quant_timescaledb pg_isready -U quant -d quant" \
    >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        echo "TimescaleDB did not become ready within ${DEPLOY_TIMEOUT_SECONDS}s." >&2
        exit 1
    fi
    sleep 2
done

STAGE="schema loading"
echo "[4/4] Loading the current schema..."
ssh "${TARGET}" "docker exec -i quant_timescaledb psql -U quant -d quant < ${REMOTE_DIR}/librae/db/timescale_init.sql" >/dev/null

echo ""
echo "Done. Remote host only has deploy/ + the schema + environment templates — not the app code."
echo "  TimescaleDB: reachable from the host itself, or via SSH tunnel."
