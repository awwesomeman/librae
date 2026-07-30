#!/usr/bin/env bash
# Deploy TimescaleDB + Grafana to a remote host by syncing the small file
# subset docker-compose.yml actually needs (deploy/ + grafana provisioning),
# then running that same file remotely. This script does not redefine the
# containers; it only controls how the required files reach the host.
#
# Deliberately syncs only .env and the empty credential template, never account
# credential files. Copy the template to .credentials/<account>.env directly
# on the remote host, so a re-run cannot clobber a live key and the key never
# has to exist on the development machine.
#
# Usage: ./deploy/cloud_deploy.sh <user>@<host>
# Requires locally: rsync, ssh, curl, python3 (scripts/dev_push_dashboard.py).
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
TUNNEL_PID=""

cleanup() {
    if [[ -n "${TUNNEL_PID}" ]]; then
        kill "${TUNNEL_PID}" 2>/dev/null || true
    fi
}

report_failure() {
    local status=$?
    echo "Cloud deployment failed during ${STAGE} (exit ${status})." >&2
    exit "${status}"
}

trap cleanup EXIT
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
set -a
# shellcheck source=/dev/null
source "${PROJECT_ROOT}/.env"
set +a
: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}"
: "${POSTGRES_APP_PASSWORD:?Set POSTGRES_APP_PASSWORD in .env}"
: "${POSTGRES_GRAFANA_PASSWORD:?Set POSTGRES_GRAFANA_PASSWORD in .env}"
: "${GF_SECURITY_ADMIN_PASSWORD:?Set GF_SECURITY_ADMIN_PASSWORD in .env}"

STAGE="file transfer"
echo "[1/6] Syncing deployment files to ${TARGET}:~/${REMOTE_DIR}/ (not the whole repo)..."
ssh "${TARGET}" "mkdir -p ${REMOTE_DIR}/deploy ${REMOTE_DIR}/librae/db ${REMOTE_DIR}/librae/app/grafana"
rsync -az "${SCRIPT_DIR}/" "${TARGET}:${REMOTE_DIR}/deploy/"
rsync -az "${PROJECT_ROOT}/librae/db/timescale_init.sql" "${TARGET}:${REMOTE_DIR}/librae/db/timescale_init.sql"
rsync -az "${PROJECT_ROOT}/librae/app/grafana/provisioning/" "${TARGET}:${REMOTE_DIR}/librae/app/grafana/provisioning/"
scp -q \
    "${PROJECT_ROOT}/.env" \
    "${PROJECT_ROOT}/.env.secrets.example" \
    "${TARGET}:${REMOTE_DIR}/"

STAGE="Compose startup"
echo "[2/6] Starting timescaledb + grafana (same docker-compose.yml as VPS-native deploy)..."
ssh "${TARGET}" "cd ${REMOTE_DIR}/deploy && docker compose --env-file ../.env up -d timescaledb grafana"

STAGE="TimescaleDB readiness"
echo "[3/6] Waiting for TimescaleDB..."
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
echo "[4/6] Loading the current schema..."
ssh "${TARGET}" "docker exec -i quant_timescaledb psql -U quant -d quant < ${REMOTE_DIR}/librae/db/timescale_init.sql" >/dev/null

STAGE="Grafana readiness"
echo "[5/6] Waiting for Grafana through a temporary SSH tunnel..."
ssh -N -L 3000:localhost:3000 "${TARGET}" &
TUNNEL_PID=$!
deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
until curl -sf http://localhost:3000/api/health >/dev/null 2>&1; do
    if ! kill -0 "${TUNNEL_PID}" 2>/dev/null; then
        echo "Grafana SSH tunnel exited before readiness." >&2
        exit 1
    fi
    if (( SECONDS >= deadline )); then
        echo "Grafana did not become ready within ${DEPLOY_TIMEOUT_SECONDS}s." >&2
        exit 1
    fi
    sleep 2
done

# Datasource is auto-provisioned from librae/app/grafana/provisioning/ (same as
# VPS-native deploy) — no separate API call needed here.
# Prefer the project venv (has `httpx` etc. via pyproject deps) over
# system python3, which may not have it installed at all.
PYTHON_BIN="python3"
if [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python3"
fi
STAGE="dashboard push"
echo "[6/6] Pushing dashboards..."
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/dev_push_dashboard.py" \
    --grafana-url "http://localhost:3000" \
    --grafana-password "${GF_SECURITY_ADMIN_PASSWORD}"

echo ""
echo "Done. Remote host only has deploy/ + librae integration assets + environment templates — not the app code."
echo "  TimescaleDB: reachable from the host itself, or via SSH tunnel."
echo "  Grafana: ssh -L 3000:localhost:3000 ${TARGET}   then open http://localhost:3000"
