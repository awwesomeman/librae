#!/usr/bin/env bash
# Build the trade image locally and push it to a registry, so the VM can
# `docker pull` it in trade.sh instead of building from source — the VM
# never needs either repo. Run this locally whenever strategy/librae code
# changes. Same image serves both sim and live mode (trade.sh start picks
# the mode).
#
# Build context is the workspace root (one level above librae/, where
# strategies/ lives as a sibling) — see Dockerfile's COPY layout.
#
# Usage: ./deploy/build_push.sh
# Requires TRADE_IMAGE in .env, e.g. ghcr.io/<github-user>/quant-trade
# One-time: docker login ghcr.io -u <github-user> (use a PAT with write:packages)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIBRAE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# librae and strategies/ are separate git repos as of 2026-07-25 — the image
# needs both, so the build context is the workspace root one level above
# librae/ (where strategies/ lives as a sibling). .env stays in librae/ —
# this script sources it below, and trade.sh/docker-compose do the same at
# container start; librae's own code never reads .env off disk itself.
BUILD_CONTEXT="$(cd "${LIBRAE_ROOT}/.." && pwd)"
STRATEGIES_DIR="${BUILD_CONTEXT}/strategies"

if [[ ! -d "${STRATEGIES_DIR}" ]]; then
    echo "Missing sibling strategy repository: ${STRATEGIES_DIR}" >&2
    echo "Place strategies/ next to librae/ before building the trade image." >&2
    exit 1
fi

if [[ -f "${LIBRAE_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${LIBRAE_ROOT}/.env"
    set +a
fi

IMAGE="${TRADE_IMAGE:?Set TRADE_IMAGE in .env, e.g. ghcr.io/<github-user>/quant-trade}"
LIBRAE_REVISION="$(git -C "${LIBRAE_ROOT}" rev-parse --verify HEAD)"
LIBRAE_VERSION="0+g${LIBRAE_REVISION:0:12}"
if [[ -n "$(git -C "${LIBRAE_ROOT}" status --porcelain --untracked-files=normal)" ]]; then
    LIBRAE_VERSION="${LIBRAE_VERSION}.dirty"
fi

echo "Building + pushing ${IMAGE}:latest (librae=${LIBRAE_VERSION}, linux/amd64 + linux/arm64)..."
# Multi-arch manifest under one tag: cloud VMs are almost always x86_64,
# but this same image is also pulled straight from a dev machine (e.g.
# Apple Silicon Macs) to run trade.sh locally against a sandbox. docker
# pull/run auto-selects the layer matching the puller's own host arch --
# no per-machine detection logic needed on our side, just publish both.
docker buildx build --platform linux/amd64,linux/arm64 \
    --build-arg LIBRAE_VERSION="${LIBRAE_VERSION}" \
    --build-arg LIBRAE_REVISION="${LIBRAE_REVISION}" \
    -t "${IMAGE}:latest" -f "${SCRIPT_DIR}/Dockerfile" --push "${BUILD_CONTEXT}"

echo "Done. On the VM: cd deploy && ./trade.sh start <strategy> [sim|live] [poll_seconds]"
