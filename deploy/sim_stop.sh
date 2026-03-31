#!/usr/bin/env bash
# Stop sim container(s).
# Usage:
#   ./deploy/sim_stop.sh <strategy>   # stop one
#   ./deploy/sim_stop.sh --all        # stop all sim containers
set -euo pipefail

if [[ "${1:-}" == "--all" ]]; then
    CONTAINERS=$(docker ps -a --filter "name=quant_sim_" --format '{{.Names}}')
    if [[ -z "${CONTAINERS}" ]]; then
        echo "No sim containers found."
        exit 0
    fi
    echo "Stopping all sim containers..."
    echo "${CONTAINERS}" | xargs docker rm -f
    echo "Done."
else
    STRATEGY="${1:?Usage: sim_stop.sh <strategy> | --all}"
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    SYMBOL=$(grep 'symbol:' "${SCRIPT_DIR}/../strategies/${STRATEGY}/config.yaml" | head -1 | awk '{print $2}' | tr '[:upper:]' '[:lower:]')
    CONTAINER="quant_sim_${STRATEGY}_${SYMBOL}"
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
        docker rm -f "${CONTAINER}" >/dev/null
        echo "Stopped ${CONTAINER}."
    else
        echo "${CONTAINER} not found."
    fi
fi
