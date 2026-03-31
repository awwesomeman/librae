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
SYMBOL=$(grep 'symbol:' "${SCRIPT_DIR}/../strategies/${STRATEGY}/config.yaml" | head -1 | awk '{print $2}' | tr '[:upper:]' '[:lower:]')
CONTAINER="quant_sim_${STRATEGY}_${SYMBOL}"

# Load .env if exists (for Telegram credentials etc.)
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${SCRIPT_DIR}/.env"
    set +a
fi

# Build image if not exists
if ! docker image inspect "${IMAGE}" &>/dev/null; then
    echo "Building sim image..."
    docker build -t "${IMAGE}" -f "${SCRIPT_DIR}/Dockerfile.sim" "${SCRIPT_DIR}/.."
fi

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
    -e TIMESCALE_DSN="${TIMESCALE_DSN:-postgresql://quant:quant_secret@timescaledb:5432/quant}" \
    -e TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}" \
    -e TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}" \
    "${IMAGE}" \
    python -m "strategies.${STRATEGY}.run" \
    --mode sim \
    --poll-interval "${POLL_INTERVAL}"

echo "Started. Logs: docker logs -f ${CONTAINER}"
