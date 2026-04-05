#!/usr/bin/env bash
# Start a sim container for a given strategy.
# Strategy params (symbol, market, etc.) come from config.yaml.
# Usage: ./deploy/sim_start.sh <strategy> [poll_interval]
# Example:
#   ./deploy/sim_start.sh trendpullback
#   ./deploy/sim_start.sh trendpullback_m5 30
set -euo pipefail

STRATEGY="${1:?Usage: sim_start.sh <strategy> [poll_interval]}"
POLL_INTERVAL="${2:-60}"
IMAGE="quant-sim"
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

# Always rebuild to pick up code changes
echo "Building sim image..."
docker build -q -t "${IMAGE}" -f "${SCRIPT_DIR}/Dockerfile.sim" "${SCRIPT_DIR}/.." >/dev/null

# Stop existing container with same name
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "Stopping existing ${CONTAINER}..."
    docker rm -f "${CONTAINER}" >/dev/null
fi

echo "Starting ${CONTAINER}: strategy=${STRATEGY}, symbol=${SYMBOL}, poll=${POLL_INTERVAL}s"

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
    --poll-interval "${POLL_INTERVAL}"

echo "Started. Logs: docker logs -f ${CONTAINER}"
