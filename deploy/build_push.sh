#!/usr/bin/env bash
# Build the trade image locally and push it to a registry, so the VM can
# `docker pull` it in trade.sh instead of building from source — the VM
# never needs the repo. Run this locally whenever strategy/librae code changes.
# Same image serves both sim and live mode (trade.sh start picks the mode).
#
# Usage: ./deploy/build_push.sh
# Requires TRADE_IMAGE in .env, e.g. ghcr.io/<github-user>/quant-trade
# One-time: docker login ghcr.io -u <github-user> (use a PAT with write:packages)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env"
    set +a
fi

IMAGE="${TRADE_IMAGE:?Set TRADE_IMAGE in .env, e.g. ghcr.io/<github-user>/quant-trade}"

echo "Building ${IMAGE}:latest..."
docker build -t "${IMAGE}:latest" -f "${SCRIPT_DIR}/Dockerfile" "${PROJECT_ROOT}"

echo "Pushing ${IMAGE}:latest..."
docker push "${IMAGE}:latest"

echo "Done. On the VM: cd deploy && ./trade.sh start <strategy> [sim|live] [poll_seconds]"
