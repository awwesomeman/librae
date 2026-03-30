#!/usr/bin/env bash
# Start a sim container for a given strategy.
# Usage: ./deploy/sim_start.sh <strategy> [symbol] [poll_interval]
# Example:
#   ./deploy/sim_start.sh trendpullback BTCUSDT
#   ./deploy/sim_start.sh meanreversion ETHUSDT 120
set -euo pipefail

STRATEGY="${1:?Usage: sim_start.sh <strategy> [symbol] [poll_interval]}"
SYMBOL="${2:-BTCUSDT}"
POLL_INTERVAL="${3:-60}"
CONTAINER="quant_sim_${STRATEGY}_${SYMBOL,,}"
IMAGE="quant-sim"
NETWORK="quant_network"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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
    -e TELEGRAM_ENABLED="${TELEGRAM_ENABLED:-false}" \
    -e TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}" \
    -e TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}" \
    "${IMAGE}" \
    python -m "strategies.${STRATEGY}.run" \
    --mode sim \
    --symbol "${SYMBOL}" \
    --poll-interval "${POLL_INTERVAL}"

echo "Started. Logs: docker logs -f ${CONTAINER}"
