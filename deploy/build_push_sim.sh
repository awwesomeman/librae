#!/usr/bin/env bash
# Build the sim image locally and push it to a registry, so the VM can
# `docker pull` it in sim_start.sh instead of building from source — the VM
# never needs the repo. Run this locally whenever strategy/librae code changes.
#
# Usage: ./deploy/build_push_sim.sh
# Requires SIM_IMAGE in .env, e.g. ghcr.io/<github-user>/quant-sim
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

IMAGE="${SIM_IMAGE:?Set SIM_IMAGE in .env, e.g. ghcr.io/<github-user>/quant-sim}"

echo "Building ${IMAGE}:latest..."
docker build -t "${IMAGE}:latest" -f "${SCRIPT_DIR}/Dockerfile.sim" "${PROJECT_ROOT}"

echo "Pushing ${IMAGE}:latest..."
docker push "${IMAGE}:latest"

echo "Done. On the VM: cd deploy && ./sim_start.sh <strategy> [poll_seconds]"
