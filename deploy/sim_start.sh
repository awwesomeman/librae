#!/usr/bin/env bash
# Start a sim container for a given strategy.
# Strategy params (symbol, market, etc.) come from config.yaml.
# Usage: ./deploy/sim_start.sh <strategy> [poll_seconds]
# Example:
#   ./deploy/sim_start.sh trendpullback
#   ./deploy/sim_start.sh trendpullback_m5 30
#
# Local dev (no SIM_IMAGE set in .env): builds the image from this checkout,
# needs the full repo — same as always.
# On a no-repo VM: set SIM_IMAGE in .env (e.g. ghcr.io/<user>/quant-sim) and
# this pulls that image instead of building — run build_push_sim.sh locally
# first whenever the code changes. Either way this script itself still needs
# to exist on whichever machine runs it (already true via cloud_deploy.sh,
# which syncs deploy/).
set -euo pipefail

STRATEGY="${1:?Usage: sim_start.sh <strategy> [poll_seconds]}"
POLL_SECONDS="${2:-60}"
IMAGE="${SIM_IMAGE:-quant-sim}"
NETWORK="quant_network"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/_common.sh"
CONTAINER=$(sim_container_name "${STRATEGY}")

# Load .env from project root (for Telegram credentials etc.)
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env"
    set +a
fi

if [[ -n "${SIM_IMAGE:-}" ]]; then
    echo "Pulling ${IMAGE}:latest..."
    docker pull -q "${IMAGE}:latest" >/dev/null
else
    echo "Building sim image locally..."
    docker build -q -t "${IMAGE}" -f "${SCRIPT_DIR}/Dockerfile.sim" "${SCRIPT_DIR}/.." >/dev/null
fi

# Stop existing container with same name
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "Stopping existing ${CONTAINER}..."
    docker rm -f "${CONTAINER}" >/dev/null
fi

echo "Starting ${CONTAINER}: strategy=${STRATEGY}, poll=${POLL_SECONDS}s"

docker run -d \
    --name "${CONTAINER}" \
    --network "${NETWORK}" \
    --restart unless-stopped \
    -e TIMESCALE_DSN="${TIMESCALE_DSN:?Set TIMESCALE_DSN in .env}" \
    -e TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}" \
    -e TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}" \
    "${IMAGE}" \
    python -m "strategies.${STRATEGY}.run" \
    --mode sim \
    --poll-seconds "${POLL_SECONDS}"

echo "Started. Logs: docker logs -f ${CONTAINER}"
