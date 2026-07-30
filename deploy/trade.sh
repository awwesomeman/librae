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
# Example (substitute a strategy that actually has a strategy.py + config.yaml
# under strategies/ — see strategies/FACTOR_ANALYSIS.md for which ones, if
# any, currently pass factor validation and are deployable):
#   ./deploy/trade.sh start <strategy>
#   ./deploy/trade.sh start <strategy> live 30
#   ./deploy/trade.sh stop <strategy> live
#
# Local dev (no TRADE_IMAGE_REF set in .env): builds from this checkout,
# needs the full repo — same as always.
# On a no-repo VM, set the digest-qualified TRADE_IMAGE_REF printed by
# build_push.sh; `start` pulls that image instead of building. Run it locally
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

# Derive a stable container name without requiring strategy source on the VM.
container_name() {
    local strategy="$1" mode="$2"
    echo "quant_${mode}_${strategy}"
}

validate_strategy_name() {
    local strategy="$1"
    if [[ ! "${strategy}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "strategy must be a Python package name, got: ${strategy}" >&2
        exit 1
    fi
}

cmd_start() {
    local strategy="${1:?Usage: trade.sh start <strategy> [mode] [poll_seconds]}"
    local mode="${2:-sim}"
    local poll_seconds="${3:-60}"
    local image=""
    local trade_timescale_dsn="${TRADE_TIMESCALE_DSN:?Set TRADE_TIMESCALE_DSN in .env}"

    if [[ "${mode}" != "sim" && "${mode}" != "live" ]]; then
        echo "mode must be 'sim' or 'live', got: ${mode}" >&2
        exit 1
    fi
    validate_strategy_name "${strategy}"

    local container
    container=$(container_name "${strategy}" "${mode}")

    if [[ "${mode}" == "live" \
        && -z "${BINANCE_API_KEY:-}" \
        && -z "${SHIOAJI_API_KEY:-}" \
        && -z "${IBKR_HOST:-}" ]]; then
        echo "live mode requires Binance, Shioaji, or IBKR settings in .env.secrets" >&2
        exit 1
    fi
    if [[ "${mode}" == "live" \
        && ( "${IBKR_HOST:-}" == "localhost" \
            || "${IBKR_HOST:-}" == "127.0.0.1" \
            || "${IBKR_HOST:-}" == "::1" ) ]]; then
        echo "IBKR_HOST cannot use container loopback; use host.docker.internal or a service name" >&2
        exit 1
    fi

    if [[ -n "${TRADE_IMAGE_REF:-}" ]]; then
        if [[ ! "${TRADE_IMAGE_REF}" =~ ^[^[:space:]]+@sha256:[0-9a-f]{64}$ ]]; then
            echo "TRADE_IMAGE_REF must be digest-qualified: <repository>@sha256:<64 hex>" >&2
            exit 1
        fi
        image="${TRADE_IMAGE_REF}"
        echo "Pulling immutable trade image ${image}..."
        docker pull -q "${image}" >/dev/null
    elif [[ -n "${TRADE_IMAGE:-}" ]]; then
        echo "TRADE_IMAGE is a publish repository, not a deployable reference." >&2
        echo "Set TRADE_IMAGE_REF to the digest printed by build_push.sh." >&2
        exit 1
    else
        image="quant-trade:local"
        local strategy_source
        strategy_source="${TRADE_STRATEGY_PATH:-../strategies}"
        if [[ "${strategy_source}" != /* ]]; then
            strategy_source="${PROJECT_ROOT}/${strategy_source}"
        fi
        if [[ ! -d "${strategy_source}" ]]; then
            echo "Strategy source directory not found: ${strategy_source}" >&2
            echo "Set TRADE_STRATEGY_PATH to the directory containing <strategy>/run.py." >&2
            exit 1
        fi
        strategy_source="$(cd "${strategy_source}" && pwd)"
        for required_file in __init__.py run.py config.yaml; do
            if [[ ! -f "${strategy_source}/${strategy}/${required_file}" ]]; then
                echo "Missing strategy file: ${strategy_source}/${strategy}/${required_file}" >&2
                exit 1
            fi
        done
        local librae_revision librae_version
        librae_revision="$(git -C "${PROJECT_ROOT}" rev-parse --verify HEAD)"
        librae_version="0+g${librae_revision:0:12}"
        if [[ -n "$(git -C "${PROJECT_ROOT}" status --porcelain --untracked-files=normal)" ]]; then
            librae_version="${librae_version}.dirty"
        fi
        echo "Building trade image locally (librae=${librae_version})..."
        docker build -q \
            --build-context "strategy_source=${strategy_source}" \
            --build-arg LIBRAE_VERSION="${librae_version}" \
            --build-arg LIBRAE_REVISION="${librae_revision}" \
            -t "${image}" -f "${SCRIPT_DIR}/Dockerfile" "${PROJECT_ROOT}" >/dev/null
    fi

    local selected_revision
    selected_revision="$(
        docker image inspect \
            --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
            "${image}"
    )"
    echo "Selected trade image: ${image}"
    if [[ -n "${selected_revision}" && "${selected_revision}" != "<no value>" ]]; then
        echo "Librae revision: ${selected_revision}"
    fi

    echo "Checking TimescaleDB connectivity from ${image} on ${NETWORK}..."
    docker run --rm \
        --network "${NETWORK}" \
        -e TIMESCALE_DSN="${trade_timescale_dsn}" \
        -e TRADE_RUN_MODULE="strategies.${strategy}.run" \
        "${image}" \
        python -c 'import importlib.util, os, psycopg2; module = os.environ["TRADE_RUN_MODULE"]; assert importlib.util.find_spec(module) is not None, f"missing strategy module: {module}"; connection = psycopg2.connect(os.environ["TIMESCALE_DSN"]); cursor = connection.cursor(); cursor.execute("SELECT 1"); assert cursor.fetchone() == (1,); connection.close()'

    local env_args=(
        -e TIMESCALE_DSN="${trade_timescale_dsn}"
        -e TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
        -e TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
    )
    local volume_args=()
    local host_args=()
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
        if [[ -n "${IBKR_HOST:-}" ]]; then
            env_args+=(
                -e IBKR_HOST="${IBKR_HOST}"
                -e IBKR_PORT="${IBKR_PORT:-7497}"
                -e IBKR_CLIENT_ID="${IBKR_CLIENT_ID:-1}"
            )
            if [[ "${IBKR_HOST}" == "host.docker.internal" ]]; then
                host_args+=(--add-host "host.docker.internal:host-gateway")
            fi
        fi
    fi

    if docker ps -a --format '{{.Names}}' | grep -q "^${container}$"; then
        echo "Stopping existing ${container}..."
        docker rm -f "${container}" >/dev/null
    fi

    echo "Starting ${container}: strategy=${strategy}, mode=${mode}, poll=${poll_seconds}s"

    docker run -d \
        --name "${container}" \
        --network "${NETWORK}" \
        --restart unless-stopped \
        "${env_args[@]}" \
        ${volume_args[@]+"${volume_args[@]}"} \
        ${host_args[@]+"${host_args[@]}"} \
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
    validate_strategy_name "${strategy}"
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
