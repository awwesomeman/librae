#!/usr/bin/env bash
# Supervise one account-specific sim or live trading container.
#
# Usage:
#   ./deploy/trade.sh start <deployment_id> <account_id> <currency> <strategy> \
#       [mode] [poll_seconds] [--config <path>] [--credentials <path>]
#   ./deploy/trade.sh inspect <deployment_id>
#   ./deploy/trade.sh restart <deployment_id>
#   ./deploy/trade.sh stop <deployment_id> [--force]
#   ./deploy/trade.sh stop --all [--force]
#   mode: sim (default, no real orders) | live (places real orders)
#
# Examples:
#   ./deploy/trade.sh start momentum-paper paper USD momentum
#   ./deploy/trade.sh start momentum-live ibkr_main USD momentum live 30 \
#       --config configs/ibkr-main.yaml \
#       --credentials .credentials/ibkr-main.env
#   ./deploy/trade.sh inspect momentum-live
#   ./deploy/trade.sh restart momentum-live
#   ./deploy/trade.sh stop momentum-live
#
# Strategy code comes from the selected immutable image. Without
# TRADE_IMAGE_REF, local development builds an image from this checkout and
# TRADE_STRATEGY_PATH. An optional host config is mounted read-only and passed
# to the strategy's existing --config option.
#
# Live mode requires one explicitly selected Docker env file. Copy
# .env.secrets.example to a per-account file under the ignored .credentials/
# directory on the machine that trades. Shioaji CA files remain under the
# ignored .secrets/ directory; only the selected account's CA file is mounted.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
NETWORK="quant_network"
READY_FILE="/tmp/librae-ready"
START_TIMEOUT_SECONDS="${TRADE_START_TIMEOUT_SECONDS:-30}"
STOP_TIMEOUT_SECONDS="${TRADE_STOP_TIMEOUT_SECONDS:-90}"

container_name() {
    local deployment_id="$1"
    echo "quant_${deployment_id}"
}

validate_deployment_id() {
    local deployment_id="$1"
    if [[ ! "${deployment_id}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
        echo "deployment_id must contain only letters, digits, '.', '_', or '-': ${deployment_id}" >&2
        exit 1
    fi
}

validate_account_id() {
    local account_id="$1"
    if [[ ! "${account_id}" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ ]]; then
        echo "account_id must contain only letters, digits, '.', '_', ':', or '-': ${account_id}" >&2
        exit 1
    fi
}

validate_currency() {
    local currency="$1"
    if [[ ! "${currency}" =~ ^[A-Z][A-Z0-9]{1,11}$ ]]; then
        echo "currency must be an uppercase code, got: ${currency}" >&2
        exit 1
    fi
}

validate_strategy_name() {
    local strategy="$1"
    if [[ ! "${strategy}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "strategy must be a Python package name, got: ${strategy}" >&2
        exit 1
    fi
}

validate_timeout() {
    local value="$1" name="$2"
    if [[ ! "${value}" =~ ^[1-9][0-9]*$ ]]; then
        echo "${name} must be a positive integer, got: ${value}" >&2
        exit 1
    fi
}

resolve_project_path() {
    local path="$1"
    if [[ "${path}" != /* && ! "${path}" =~ ^[A-Za-z]:[\\/] ]]; then
        path="${PROJECT_ROOT}/${path}"
    fi
    printf '%s\n' "${path}"
}

read_env_file_value() {
    local file="$1" key="$2"
    awk -v key="${key}" '
        {
            sub(/\r$/, "")
            prefix = key "="
            if (index($0, prefix) == 1) {
                print substr($0, length(prefix) + 1)
                exit
            }
        }
    ' "${file}"
}

container_exists() {
    local container="$1"
    docker ps -a --format '{{.Names}}' | grep -Fxq "${container}"
}

container_status() {
    docker inspect --format '{{.State.Status}}' "$1"
}

container_running() {
    [[ "$(docker inspect --format '{{.State.Running}}' "$1")" == "true" ]]
}

container_ready_file() {
    local container="$1" ready_file
    ready_file="$(
        docker inspect \
            --format '{{ index .Config.Labels "io.librae.ready_file" }}' \
            "${container}"
    )"
    if [[ -z "${ready_file}" || "${ready_file}" == "<no value>" ]]; then
        ready_file="${READY_FILE}"
    fi
    printf '%s\n' "${ready_file}"
}

wait_until_stopped() {
    local container="$1"
    local deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
    while container_running "${container}"; do
        if (( SECONDS >= deadline )); then
            echo "${container} did not stop within ${STOP_TIMEOUT_SECONDS}s." >&2
            echo "Inspect it, then retry with --force." >&2
            return 1
        fi
        sleep 1
    done
}

stop_container() {
    local container="$1" force="${2:-false}"
    if ! container_running "${container}"; then
        return 0
    fi
    if [[ "${force}" == "true" ]]; then
        docker kill --signal KILL "${container}" >/dev/null
        return 0
    fi
    docker stop --signal TERM --time -1 "${container}" >/dev/null &
    local stop_pid=$!
    if wait_until_stopped "${container}"; then
        wait "${stop_pid}"
        return 0
    fi
    kill "${stop_pid}" 2>/dev/null || true
    wait "${stop_pid}" 2>/dev/null || true
    return 1
}

wait_until_ready() {
    local container="$1" previous_marker="${2:-}"
    local initial_restart_count deadline status restart_count marker ready_file
    ready_file="$(container_ready_file "${container}")"
    initial_restart_count="$(docker inspect --format '{{.RestartCount}}' "${container}")"
    deadline=$((SECONDS + START_TIMEOUT_SECONDS))
    while true; do
        status="$(container_status "${container}")"
        restart_count="$(docker inspect --format '{{.RestartCount}}' "${container}")"
        if [[ "${status}" == "running" ]]; then
            marker="$(docker exec "${container}" cat "${ready_file}" 2>/dev/null || true)"
            if [[ -n "${marker}" && "${marker}" != "${previous_marker}" ]]; then
                return 0
            fi
        fi
        if [[ "${status}" == "exited" || "${status}" == "dead" \
            || "${status}" == "restarting" || "${restart_count}" != "${initial_restart_count}" ]]; then
            echo "${container} failed before becoming ready (status=${status}, restarts=${restart_count})." >&2
            docker logs --tail 50 "${container}" >&2 || true
            return 1
        fi
        if (( SECONDS >= deadline )); then
            echo "${container} did not become ready within ${START_TIMEOUT_SECONDS}s." >&2
            docker logs --tail 50 "${container}" >&2 || true
            return 1
        fi
        sleep 1
    done
}

validate_existing_binding() {
    local container="$1" label="$2" expected="$3"
    local actual
    actual="$(
        docker inspect \
            --format "{{ index .Config.Labels \"io.librae.${label}\" }}" \
            "${container}"
    )"
    if [[ "${actual}" != "${expected}" ]]; then
        echo "deployment_id is already bound to ${label}=${actual}; expected ${expected}" >&2
        echo "Stop the old deployment before reusing its id for a different binding." >&2
        exit 1
    fi
}

cmd_start() {
    local usage="Usage: trade.sh start <deployment_id> <account_id> <currency> <strategy> [mode] [poll_seconds] [--config <path>] [--credentials <path>]"
    local deployment_id="${1:?${usage}}"
    local account_id="${2:?${usage}}"
    local currency="${3:?${usage}}"
    local strategy="${4:?${usage}}"
    shift 4

    local mode="sim"
    local poll_seconds="60"
    if [[ $# -gt 0 && "${1}" != --* ]]; then
        mode="$1"
        shift
    fi
    if [[ $# -gt 0 && "${1}" != --* ]]; then
        poll_seconds="$1"
        shift
    fi

    local config_file=""
    local credentials_file=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --config)
                [[ $# -ge 2 ]] || { echo "${usage}" >&2; exit 1; }
                config_file="$2"
                shift 2
                ;;
            --credentials)
                [[ $# -ge 2 ]] || { echo "${usage}" >&2; exit 1; }
                credentials_file="$2"
                shift 2
                ;;
            *)
                echo "Unknown start option: $1" >&2
                echo "${usage}" >&2
                exit 1
                ;;
        esac
    done

    if [[ "${mode}" != "sim" && "${mode}" != "live" ]]; then
        echo "mode must be 'sim' or 'live', got: ${mode}" >&2
        exit 1
    fi
    validate_deployment_id "${deployment_id}"
    validate_account_id "${account_id}"
    validate_currency "${currency}"
    validate_strategy_name "${strategy}"
    validate_timeout "${START_TIMEOUT_SECONDS}" "TRADE_START_TIMEOUT_SECONDS"
    validate_timeout "${STOP_TIMEOUT_SECONDS}" "TRADE_STOP_TIMEOUT_SECONDS"

    local container
    container="$(container_name "${deployment_id}")"
    local ready_file="${READY_FILE}-${deployment_id}"
    local trade_timescale_dsn="${TRADE_TIMESCALE_DSN:?Set TRADE_TIMESCALE_DSN in .env}"

    local credential_args=()
    local secret_mount_args=()
    local host_args=()
    if [[ "${mode}" == "live" ]]; then
        if [[ -z "${credentials_file}" ]]; then
            echo "live mode requires --credentials <account-env-file>" >&2
            exit 1
        fi
        credentials_file="$(resolve_project_path "${credentials_file}")"
        if [[ ! -f "${credentials_file}" ]]; then
            echo "Credential file not found: ${credentials_file}" >&2
            exit 1
        fi
        credential_args+=(--env-file "${credentials_file}")

        local ibkr_host
        ibkr_host="$(read_env_file_value "${credentials_file}" "IBKR_HOST")"
        if [[ "${ibkr_host}" == "host.docker.internal" ]]; then
            host_args+=(--add-host "host.docker.internal:host-gateway")
        fi

        local shioaji_api_key
        shioaji_api_key="$(read_env_file_value "${credentials_file}" "SHIOAJI_API_KEY")"
        if [[ -n "${shioaji_api_key}" ]]; then
            local shioaji_ca_path
            shioaji_ca_path="$(read_env_file_value "${credentials_file}" "SHIOAJI_CA_PATH")"
            if [[ "${shioaji_ca_path}" != .secrets/* || "${shioaji_ca_path}" == *..* ]]; then
                echo "SHIOAJI_CA_PATH must be a relative path under .secrets/" >&2
                exit 1
            fi
            local shioaji_ca_file="${PROJECT_ROOT}/${shioaji_ca_path}"
            if [[ ! -f "${shioaji_ca_file}" ]]; then
                echo "Shioaji CA file not found: ${shioaji_ca_file}" >&2
                exit 1
            fi
            secret_mount_args+=(-v "${shioaji_ca_file}:/app/${shioaji_ca_path}:ro")
        fi
    elif [[ -n "${credentials_file}" ]]; then
        echo "--credentials is only valid in live mode" >&2
        exit 1
    fi

    local container_config="/app/strategies/${strategy}/config.yaml"
    local config_mount_args=()
    local runner_config_args=()
    if [[ -n "${config_file}" ]]; then
        config_file="$(resolve_project_path "${config_file}")"
        if [[ ! -f "${config_file}" ]]; then
            echo "Strategy config file not found: ${config_file}" >&2
            exit 1
        fi
        container_config="/app/deployment/config.yaml"
        config_mount_args+=(-v "${config_file}:${container_config}:ro")
        runner_config_args+=(--config "${container_config}")
    fi

    local image=""
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
        local -a uv_build_args=()
        [[ -z "${UV_DEFAULT_INDEX:-}" ]] || uv_build_args+=(--build-arg UV_DEFAULT_INDEX)
        [[ -z "${UV_INDEX:-}" ]] || uv_build_args+=(--build-arg UV_INDEX)
        [[ -z "${UV_FIND_LINKS:-}" ]] || uv_build_args+=(--build-arg UV_FIND_LINKS)
        echo "Building trade image locally (librae=${librae_version})..."
        docker build -q \
            "${uv_build_args[@]}" \
            --build-context "strategy_source=${strategy_source}" \
            --build-arg LIBRAE_VERSION="${librae_version}" \
            --build-arg LIBRAE_REVISION="${librae_revision}" \
            -t "${image}" -f "${SCRIPT_DIR}/Dockerfile" "${PROJECT_ROOT}" >/dev/null
    fi

    local selected_revision runtime_revision
    selected_revision="$(
        docker image inspect \
            --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
            "${image}"
    )"
    runtime_revision="$(
        docker image inspect \
            --format '{{.Id}}' \
            "${image}"
    )"
    if [[ -z "${runtime_revision}" ]]; then
        echo "Cannot resolve runtime revision from selected image: ${image}" >&2
        exit 1
    fi
    echo "Selected trade image: ${image}"
    echo "Runtime revision: ${runtime_revision}"
    if [[ -n "${selected_revision}" && "${selected_revision}" != "<no value>" ]]; then
        echo "Librae revision: ${selected_revision}"
    fi

    echo "Checking strategy account and TimescaleDB connectivity from ${image} on ${NETWORK}..."
    docker run --rm \
        --network "${NETWORK}" \
        "${credential_args[@]}" \
        "${config_mount_args[@]}" \
        "${secret_mount_args[@]}" \
        -e TIMESCALE_DSN="${trade_timescale_dsn}" \
        -e TRADE_RUN_MODULE="strategies.${strategy}.run" \
        -e TRADE_CONFIG_PATH="${container_config}" \
        -e TRADE_ACCOUNT_ID="${account_id}" \
        -e TRADE_CURRENCY="${currency}" \
        -e TRADE_MODE="${mode}" \
        "${image}" \
        python -c '
import importlib.util
import os
from pathlib import Path

import psycopg2
import yaml

module = os.environ["TRADE_RUN_MODULE"]
if importlib.util.find_spec(module) is None:
    raise SystemExit(f"missing strategy module: {module}")

config_path = Path(os.environ["TRADE_CONFIG_PATH"])
config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
strategy_config = config.get("strategy")
account = strategy_config.get("account") if isinstance(strategy_config, dict) else None
actual_account_id = account.get("account_id") if isinstance(account, dict) else None
expected_account_id = os.environ["TRADE_ACCOUNT_ID"]
if actual_account_id != expected_account_id:
    raise SystemExit(
        f"strategy account_id mismatch: expected {expected_account_id!r}, "
        f"found {actual_account_id!r} in {config_path}"
    )

actual_currency = account.get("currency") if isinstance(account, dict) else None
expected_currency = os.environ["TRADE_CURRENCY"]
if actual_currency != expected_currency:
    raise SystemExit(
        f"strategy currency mismatch: expected {expected_currency!r}, "
        f"found {actual_currency!r} in {config_path}"
    )

if os.environ["TRADE_MODE"] == "live":
    if not any(
        os.environ.get(name)
        for name in ("BINANCE_API_KEY", "SHIOAJI_API_KEY", "IBKR_HOST")
    ):
        raise SystemExit(
            "live credentials must define BINANCE_API_KEY, SHIOAJI_API_KEY, or IBKR_HOST"
        )
    if os.environ.get("BINANCE_API_KEY") and not os.environ.get("BINANCE_API_SECRET"):
        raise SystemExit("BINANCE_API_SECRET is required alongside BINANCE_API_KEY")
    if os.environ.get("SHIOAJI_API_KEY") and not os.environ.get("SHIOAJI_SECRET_KEY"):
        raise SystemExit("SHIOAJI_SECRET_KEY is required alongside SHIOAJI_API_KEY")
    if os.environ.get("IBKR_HOST") in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit(
            "IBKR_HOST cannot use container loopback; "
            "use host.docker.internal or a service name"
        )

connection = psycopg2.connect(os.environ["TIMESCALE_DSN"])
try:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        if cursor.fetchone() != (1,):
            raise SystemExit("TimescaleDB preflight returned an unexpected result")
finally:
    connection.close()
'

    if container_exists "${container}"; then
        validate_existing_binding "${container}" "account_id" "${account_id}"
        validate_existing_binding "${container}" "currency" "${currency}"
        validate_existing_binding "${container}" "strategy" "${strategy}"
        validate_existing_binding "${container}" "mode" "${mode}"
    fi

    if [[ "${mode}" == "live" ]]; then
        local legacy_container legacy_managed
        while IFS= read -r legacy_container; do
            [[ -z "${legacy_container}" ]] && continue
            legacy_managed="$(
                docker inspect \
                    --format '{{ index .Config.Labels "io.librae.managed" }}' \
                    "${legacy_container}"
            )"
            if [[ "${legacy_managed}" != "true" ]]; then
                echo "legacy live container ${legacy_container} has no account binding" >&2
                echo "Stop it explicitly before starting account-specific live deployments." >&2
                exit 1
            fi
        done < <(
            docker ps \
                --filter "name=quant_live_" \
                --format '{{.Names}}'
        )

        local conflicting_container
        while IFS= read -r conflicting_container; do
            if [[ -n "${conflicting_container}" && "${conflicting_container}" != "${container}" ]]; then
                echo "account_id ${account_id} is already owned by live deployment ${conflicting_container}" >&2
                exit 1
            fi
        done < <(
            docker ps \
                --filter "label=io.librae.managed=true" \
                --filter "label=io.librae.mode=live" \
                --filter "label=io.librae.account_id=${account_id}" \
                --format '{{.Names}}'
        )
    fi

    local env_args=(
        -e TIMESCALE_DSN="${trade_timescale_dsn}"
        -e TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
        -e TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
        -e LIBRAE_READY_FILE="${ready_file}"
    )
    if container_exists "${container}"; then
        echo "Stopping existing ${container}..."
        stop_container "${container}"
        docker rm "${container}" >/dev/null
    fi

    local runtime_revision_args=()
    if [[ "${mode}" == "live" ]]; then
        runtime_revision_args+=(--runtime-revision "${runtime_revision}")
    fi

    echo "Starting ${container}: account=${account_id}, currency=${currency}, strategy=${strategy}, mode=${mode}, poll=${poll_seconds}s"

    docker run -d \
        --name "${container}" \
        --network "${NETWORK}" \
        --restart unless-stopped \
        --label "io.librae.managed=true" \
        --label "io.librae.deployment_id=${deployment_id}" \
        --label "io.librae.account_id=${account_id}" \
        --label "io.librae.currency=${currency}" \
        --label "io.librae.strategy=${strategy}" \
        --label "io.librae.mode=${mode}" \
        --label "io.librae.runtime_revision=${runtime_revision}" \
        --label "io.librae.ready_file=${ready_file}" \
        "${credential_args[@]}" \
        "${env_args[@]}" \
        "${config_mount_args[@]}" \
        "${secret_mount_args[@]}" \
        "${host_args[@]}" \
        "${image}" \
        python -m "strategies.${strategy}.run" \
        --mode "${mode}" \
        --poll-seconds "${poll_seconds}" \
        "${runtime_revision_args[@]}" \
        "${runner_config_args[@]}"

    wait_until_ready "${container}" ""
    echo "Ready. Logs: docker logs -f ${container}"
}

cmd_inspect() {
    local deployment_id="${1:?Usage: trade.sh inspect <deployment_id>}"
    validate_deployment_id "${deployment_id}"
    local container
    container="$(container_name "${deployment_id}")"
    if ! container_exists "${container}"; then
        echo "${container} not found." >&2
        return 1
    fi

    local account_id currency status restart_count process_id exit_code reason ready run_id phase
    local ready_file
    account_id="$(docker inspect --format '{{ index .Config.Labels "io.librae.account_id" }}' "${container}")"
    currency="$(docker inspect --format '{{ index .Config.Labels "io.librae.currency" }}' "${container}")"
    status="$(container_status "${container}")"
    restart_count="$(docker inspect --format '{{.RestartCount}}' "${container}")"
    process_id="$(docker inspect --format '{{.State.Pid}}' "${container}")"
    exit_code="$(docker inspect --format '{{.State.ExitCode}}' "${container}")"
    reason="$(docker inspect --format '{{.State.Error}}' "${container}")"
    ready_file="$(container_ready_file "${container}")"
    ready="false"
    run_id=""
    if [[ "${status}" == "running" ]]; then
        run_id="$(docker exec "${container}" cat "${ready_file}" 2>/dev/null || true)"
        if [[ -n "${run_id}" ]]; then
            ready="true"
            run_id="${run_id%%:*}"
        fi
    fi

    if [[ "${ready}" != "true" && "${restart_count}" != "0" ]]; then
        phase="failed"
    else
        case "${status}" in
            running)    [[ "${ready}" == "true" ]] && phase="running" || phase="starting" ;;
            created)    phase="starting" ;;
            restarting) phase="failed" ;;
            exited)     [[ "${exit_code}" == "0" ]] && phase="stopped" || phase="failed" ;;
            dead)       phase="failed" ;;
            *)          phase="unknown" ;;
        esac
    fi
    if [[ "${status}" == "running" || "${status}" == "created" ]]; then
        exit_code=""
    fi
    if [[ "${process_id}" == "0" ]]; then
        process_id=""
    fi

    printf '%s\n' \
        "deployment_id=${deployment_id}" \
        "account_id=${account_id}" \
        "currency=${currency}" \
        "phase=${phase}" \
        "run_id=${run_id}" \
        "process_id=${process_id}" \
        "exit_code=${exit_code}" \
        "restart_count=${restart_count}" \
        "reason=${reason}"
}

read_ready_marker() {
    local container="$1" marker_file marker="" ready_file
    ready_file="$(container_ready_file "${container}")"
    if container_running "${container}"; then
        docker exec "${container}" cat "${ready_file}" 2>/dev/null || true
        return 0
    fi
    marker_file="$(mktemp)"
    if docker cp "${container}:${ready_file}" "${marker_file}" >/dev/null 2>&1; then
        marker="$(cat "${marker_file}")"
    fi
    rm -f -- "${marker_file}"
    printf '%s\n' "${marker}"
}

cmd_restart() {
    local deployment_id="${1:?Usage: trade.sh restart <deployment_id>}"
    validate_deployment_id "${deployment_id}"
    validate_timeout "${START_TIMEOUT_SECONDS}" "TRADE_START_TIMEOUT_SECONDS"
    validate_timeout "${STOP_TIMEOUT_SECONDS}" "TRADE_STOP_TIMEOUT_SECONDS"
    local container
    container="$(container_name "${deployment_id}")"
    if ! container_exists "${container}"; then
        echo "${container} not found." >&2
        return 1
    fi
    validate_existing_binding "${container}" "managed" "true"

    local previous_marker
    previous_marker="$(read_ready_marker "${container}")"
    stop_container "${container}"
    docker start "${container}" >/dev/null
    wait_until_ready "${container}" "${previous_marker}"
    cmd_inspect "${deployment_id}"
}

cmd_stop() {
    local force="false"
    if [[ "${*: -1}" == "--force" ]]; then
        force="true"
        set -- "${@:1:$(($# - 1))}"
    fi
    validate_timeout "${STOP_TIMEOUT_SECONDS}" "TRADE_STOP_TIMEOUT_SECONDS"

    if [[ "${1:-}" == "--all" ]]; then
        local containers=()
        local container
        while IFS= read -r container; do
            [[ -n "${container}" ]] && containers+=("${container}")
        done < <(
            docker ps -a \
                --filter "label=io.librae.managed=true" \
                --format '{{.Names}}'
        )
        if [[ ${#containers[@]} -eq 0 ]]; then
            echo "No trade containers found."
            return 0
        fi
        echo "Stopping all trade containers..."
        for container in "${containers[@]}"; do
            stop_container "${container}" "${force}"
        done
        echo "Done."
        return 0
    fi

    local deployment_id="${1:?Usage: trade.sh stop <deployment_id> [--force] | --all [--force]}"
    validate_deployment_id "${deployment_id}"
    local container
    container="$(container_name "${deployment_id}")"
    if container_exists "${container}"; then
        stop_container "${container}" "${force}"
        echo "Stopped ${container}."
    else
        echo "${container} not found." >&2
        return 1
    fi
}

SUBCOMMAND="${1:?Usage: trade.sh <start|stop|inspect|restart> ...}"
shift

# Load non-trading deployment settings from the project root. Live credentials
# are never sourced by this shell; start passes the explicitly selected file to
# Docker with --env-file.
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env"
    set +a
fi

case "${SUBCOMMAND}" in
    start) cmd_start "$@" ;;
    stop)  cmd_stop "$@" ;;
    inspect) cmd_inspect "$@" ;;
    restart) cmd_restart "$@" ;;
    *)
        echo "Unknown subcommand: ${SUBCOMMAND} (expected start|stop|inspect|restart)" >&2
        exit 1
        ;;
esac
