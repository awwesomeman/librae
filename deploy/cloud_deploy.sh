#!/usr/bin/env bash
# Deploy TimescaleDB + Grafana to a remote host by syncing the small file
# subset docker-compose.yml actually needs (deploy/ + grafana provisioning),
# then running that SAME docker-compose.yml remotely — the one already used
# for VPS-native deployment (`cd deploy && docker compose up -d`). One
# source of truth for container specs, so there's nothing to drift: this
# script never redefines what the containers look like, only how the files
# get there.
#
# Deliberately only syncs .env, never .env.secrets (trading-enabled API
# keys) — that file must be created directly on the remote host (see
# .env.secrets.example) so a re-run of this script can never clobber a live
# key with an empty local value, and the key never has to exist on the dev
# machine at all.
#
# Usage: ./deploy/cloud_deploy.sh <user>@<host>
# Requires locally: rsync, ssh, curl, python3 (scripts/dev_push_dashboard.py).
# Requires on the remote host: Docker + docker-compose-plugin only, e.g.
# `apt install -y docker.io docker-compose-plugin` — no git, no repo.
#
# Disk sizing is on the operator: named volumes land on Docker's data-root
# (default /var/lib/docker, i.e. the root disk). Multi-disk host? Repoint
# both dockerd (/etc/docker/daemon.json "data-root") and, if containerd runs
# standalone, /etc/containerd/config.toml "root" — before running this —
# or the DB fills the root disk and crashes regardless of other disks' space.
set -euo pipefail

TARGET="${1:?Usage: cloud_deploy.sh <user>@<host>}"
REMOTE_DIR="quant-deploy"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load .env from project root (for POSTGRES_PASSWORD, GF_SECURITY_ADMIN_PASSWORD)
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env"
    set +a
fi
: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}"
: "${GF_SECURITY_ADMIN_PASSWORD:?Set GF_SECURITY_ADMIN_PASSWORD in .env}"

echo "[1/4] Syncing deployment files to ${TARGET}:~/${REMOTE_DIR}/ (not the whole repo)..."
ssh "${TARGET}" "mkdir -p ${REMOTE_DIR}/deploy ${REMOTE_DIR}/db ${REMOTE_DIR}/app/grafana"
rsync -az "${SCRIPT_DIR}/" "${TARGET}:${REMOTE_DIR}/deploy/"
rsync -az "${PROJECT_ROOT}/db/timescale_init.sql" "${TARGET}:${REMOTE_DIR}/db/timescale_init.sql"
rsync -az "${PROJECT_ROOT}/app/grafana/provisioning/" "${TARGET}:${REMOTE_DIR}/app/grafana/provisioning/"
scp -q "${PROJECT_ROOT}/.env" "${TARGET}:${REMOTE_DIR}/.env"

echo "[2/4] Starting timescaledb + grafana (same docker-compose.yml as VPS-native deploy)..."
ssh "${TARGET}" "cd ${REMOTE_DIR}/deploy && docker compose --env-file ../.env up -d timescaledb grafana"

echo "[3/4] Waiting for TimescaleDB, loading schema..."
ssh "${TARGET}" "until docker exec quant_timescaledb pg_isready -U quant -d quant >/dev/null 2>&1; do sleep 2; done"
ssh "${TARGET}" "docker exec -i quant_timescaledb psql -U quant -d quant < ${REMOTE_DIR}/db/timescale_init.sql" >/dev/null

echo "[4/4] Pushing dashboards via a temporary SSH tunnel..."
ssh -N -L 3000:localhost:3000 "${TARGET}" &
TUNNEL_PID=$!
trap 'kill "${TUNNEL_PID}" 2>/dev/null' EXIT
until curl -sf http://localhost:3000/api/health >/dev/null 2>&1; do
    sleep 2
done

# Datasource is auto-provisioned from app/grafana/provisioning/ (same as
# VPS-native deploy) — no separate API call needed here.
# Prefer the project venv (has `requests` etc. via pyproject deps) over
# system python3, which may not have it installed at all.
PYTHON_BIN="python3"
if [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python3"
fi
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/dev_push_dashboard.py" \
    --grafana-url "http://localhost:3000" \
    --grafana-password "${GF_SECURITY_ADMIN_PASSWORD}"

echo ""
echo "Done. Remote host only has deploy/ + app/grafana/provisioning/ + .env — not the app code."
echo "  TimescaleDB: reachable from the host itself, or via SSH tunnel."
echo "  Grafana: ssh -L 3000:localhost:3000 ${TARGET}   then open http://localhost:3000"
