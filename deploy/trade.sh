#!/usr/bin/env bash
# Start/stop a sim or live trading container for a strategy.
# Strategy params (symbol, market, etc.) come from config.yaml.
#
# Usage:
#   ./deploy/trade.sh start <strategy> [mode] [poll_seconds]
#   ./deploy/trade.sh stop  <strategy> [mode]
#   ./deploy/trade.sh stop  --all
#   mode: sim (default, no real orders) | live (places real orders)
#
# Example:
#   ./deploy/trade.sh start trendpullback
#   ./deploy/trade.sh start trendpullback live 30
#   ./deploy/trade.sh stop trendpullback live
#
# Local dev (no TRADE_IMAGE set in .env): builds the image from this checkout,
# needs the full repo — same as always.
# On a no-repo VM: set TRADE_IMAGE in .env (e.g. ghcr.io/<user>/quant-trade) and
# `start` pulls that image instead of building — run build_push.sh locally
# first whenever the code changes. Either way this script itself still needs
# to exist on whichever machine runs it (already true via cloud_deploy.sh,
# which syncs deploy/).
#
# live mode injects whichever credential set is present in .env.secrets (see
# .env.secrets.example — never synced by cloud_deploy.sh, create it directly
# on this machine): BINANCE_* for CryptoAdapter, SHIOAJI_* for ShioajiAdapter
# (see librae/live/engine.py — market is auto-detected per symbol, this
# script doesn't need to know which). Shioaji additionally needs its CA
# cert file mounted into the container — trade.sh bind-mounts
# ${PROJECT_ROOT}/.secrets (read-only) whenever SHIOAJI_CA_PATH points at a
# file that actually exists there, so SHIOAJI_CA_PATH must stay a relative
# path under .secrets/ (matches .env.secrets.example's default).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NETWORK="quant_network"

# Derive container name from strategy config + mode.
container_name() {
    local strategy="$1" mode="$2"
    local config="${SCRIPT_DIR}/../strategies/${strategy}/config.yaml"
    # On a no-repo VM there's no strategies/ tree to read symbol from —
    # fall back to strategy name alone (still unique per running strategy+mode).
    if [[ ! -f "${config}" ]]; then
        echo "quant_${mode}_${strategy}"
        return
    fi
    local symbol
    symbol=$(grep 'symbol:' "${config}" | head -1 | awk '{print $2}' | tr '[:upper:]' '[:lower:]')
    echo "quant_${mode}_${strategy}_${symbol}"
}

cmd_start() {
    local strategy="${1:?Usage: trade.sh start <strategy> [mode] [poll_seconds]}"
    local mode="${2:-sim}"
    local poll_seconds="${3:-60}"
    local image="${TRADE_IMAGE:-quant-trade}"

    if [[ "${mode}" != "sim" && "${mode}" != "live" ]]; then
        echo "mode must be 'sim' or 'live', got: ${mode}" >&2
        exit 1
    fi

    local container
    container=$(container_name "${strategy}" "${mode}")

    if [[ "${mode}" == "live" && -z "${BINANCE_API_KEY:-}" && -z "${SHIOAJI_API_KEY:-}" ]]; then
        echo "live mode requires BINANCE_API_KEY or SHIOAJI_API_KEY in .env.secrets" >&2
        exit 1
    fi

    if [[ -n "${TRADE_IMAGE:-}" ]]; then
        echo "Pulling ${image}:latest..."
        docker pull -q "${image}:latest" >/dev/null
    else
        echo "Building trade image locally..."
        docker build -q -t "${image}" -f "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}/.." >/dev/null
    fi

    if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
        echo "Stopping existing ${container}..."
        docker rm -f "${container}" >/dev/null
    fi

    echo "Starting ${container}: strategy=${strategy}, mode=${mode}, poll=${poll_seconds}s"

    local env_args=(
        -e TIMESCALE_DSN="${TIMESCALE_DSN:?Set TIMESCALE_DSN in .env}"
        -e TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
        -e TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
    )
    local volume_args=()
    if [[ "${mode}" == "live" ]]; then
        if [[ -n "${BINANCE_API_KEY:-}" ]]; then
            env_args+=(
                -e BINANCE_API_KEY="${BINANCE_API_KEY}"
                -e BINANCE_API_SECRET="${BINANCE_API_SECRET:?live mode requires BINANCE_API_SECRET alongside BINANCE_API_KEY}"
                -e BINANCE_EXCHANGE_ID="${BINANCE_EXCHANGE_ID:-binance}"
                -e BINANCE_SANDBOX="${BINANCE_SANDBOX:-false}"
            )
        fi
        if [[ -n "${SHIOAJI_API_KEY:-}" ]]; then
            env_args+=(
                -e SHIOAJI_API_KEY="${SHIOAJI_API_KEY}"
                -e SHIOAJI_SECRET_KEY="${SHIOAJI_SECRET_KEY:?live mode requires SHIOAJI_SECRET_KEY alongside SHIOAJI_API_KEY}"
                -e SHIOAJI_PERSON_ID="${SHIOAJI_PERSON_ID:-}"
                -e SHIOAJI_CA_PATH="${SHIOAJI_CA_PATH:-}"
                -e SHIOAJI_CA_PASSWORD="${SHIOAJI_CA_PASSWORD:-}"
                -e SHIOAJI_SANDBOX="${SHIOAJI_SANDBOX:-false}"
            )
            if [[ -n "${SHIOAJI_CA_PATH:-}" && -f "${PROJECT_ROOT}/${SHIOAJI_CA_PATH}" ]]; then
                volume_args+=(-v "${PROJECT_ROOT}/.secrets:/app/.secrets:ro")
            fi
        fi
    fi

    docker run -d \
        --name "${container}" \
        --network "${NETWORK}" \
        --restart unless-stopped \
        "${env_args[@]}" \
        "${volume_args[@]}" \
        "${image}" \
        python -m "strategies.${strategy}.run" \
        --mode "${mode}" \
        --poll-seconds "${poll_seconds}"

    echo "Started. Logs: docker logs -f ${container}"
}

cmd_stop() {
    if [[ "${1:-}" == "--all" ]]; then
        local containers
        containers=$(docker ps -a --filter "name=quant_sim_" --filter "name=quant_live_" --format '{{.Names}}')
        if [[ -z "${containers}" ]]; then
            echo "No trade containers found."
            return 0
        fi
        echo "Stopping all trade containers..."
        echo "${containers}" | xargs docker rm -f
        echo "Done."
        return 0
    fi

    local strategy="${1:?Usage: trade.sh stop <strategy> [mode] | --all}"
    local mode="${2:-sim}"
    local container
    container=$(container_name "${strategy}" "${mode}")
    if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
        docker rm -f "${container}" >/dev/null
        echo "Stopped ${container}."
    else
        echo "${container} not found."
    fi
}

SUBCOMMAND="${1:?Usage: trade.sh <start|stop> ...}"
shift

# Load .env from project root (for Telegram/exchange credentials etc.), then
# .env.secrets (trading-enabled keys — see .env.secrets.example, never synced
# by cloud_deploy.sh, only ever created directly on the machine that trades).
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env"
    set +a
fi
if [[ -f "${PROJECT_ROOT}/.env.secrets" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env.secrets"
    set +a
fi

case "${SUBCOMMAND}" in
    start) cmd_start "$@" ;;
    stop)  cmd_stop "$@" ;;
    *)
        echo "Unknown subcommand: ${SUBCOMMAND} (expected start|stop)" >&2
        exit 1
        ;;
esac
