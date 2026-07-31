#!/usr/bin/env bash
# Build the trade image locally and push an immutable source-revision tag to a
# registry. The script prints the digest-qualified TRADE_IMAGE_REF selected by
# trade.sh; it never updates deployment configuration automatically.
#
# TRADE_STRATEGY_PATH selects the caller-owned source directory. Relative
# paths resolve from the Librae checkout; the default is ../strategies.
# TRADE_PLATFORMS may narrow validation builds; production defaults to both
# supported image architectures.
#
# Usage: ./deploy/build_push.sh
# Requires TRADE_IMAGE in .env, e.g. ghcr.io/<github-user>/quant-trade.
# One-time: docker login ghcr.io -u <github-user> (use a PAT with write:packages)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIBRAE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${LIBRAE_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${LIBRAE_ROOT}/.env"
    set +a
fi

STRATEGY_SOURCE="${TRADE_STRATEGY_PATH:-../strategies}"
if [[ "${STRATEGY_SOURCE}" != /* ]]; then
    STRATEGY_SOURCE="${LIBRAE_ROOT}/${STRATEGY_SOURCE}"
fi
if [[ ! -d "${STRATEGY_SOURCE}" ]]; then
    echo "Strategy source directory not found: ${STRATEGY_SOURCE}" >&2
    echo "Set TRADE_STRATEGY_PATH to the directory containing <strategy>/run.py." >&2
    exit 1
fi
STRATEGY_SOURCE="$(cd "${STRATEGY_SOURCE}" && pwd)"

IMAGE="${TRADE_IMAGE:?Set TRADE_IMAGE in .env, e.g. ghcr.io/<github-user>/quant-trade}"
if [[ "${IMAGE}" == *@* || "${IMAGE##*/}" == *:* ]]; then
    echo "TRADE_IMAGE must be a repository without a tag or digest: ${IMAGE}" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required to read the Docker build metadata." >&2
    exit 1
fi

LIBRAE_REVISION="$(git -C "${LIBRAE_ROOT}" rev-parse --verify HEAD)"
LIBRAE_VERSION="0+g${LIBRAE_REVISION:0:12}"
SOURCE_TAG="librae-${LIBRAE_REVISION:0:12}"
if [[ -n "$(git -C "${LIBRAE_ROOT}" status --porcelain --untracked-files=normal)" ]]; then
    LIBRAE_VERSION="${LIBRAE_VERSION}.dirty"
    SOURCE_TAG="${SOURCE_TAG}-dirty"
fi
IMAGE_TAG="${IMAGE}:${SOURCE_TAG}"
PLATFORMS="${TRADE_PLATFORMS:-linux/amd64,linux/arm64}"
METADATA_FILE="$(mktemp)"
trap 'rm -f "${METADATA_FILE}"' EXIT

echo "Building + pushing ${IMAGE_TAG} (${PLATFORMS//,/ + })..."
echo "Librae revision: ${LIBRAE_REVISION}"
# Production publishes both architectures under one source-revision tag.
# Docker pull/run later selects the matching platform from the manifest.
docker buildx build --platform "${PLATFORMS}" \
    --build-context "strategy_source=${STRATEGY_SOURCE}" \
    --build-arg LIBRAE_VERSION="${LIBRAE_VERSION}" \
    --build-arg LIBRAE_REVISION="${LIBRAE_REVISION}" \
    --metadata-file "${METADATA_FILE}" \
    -t "${IMAGE_TAG}" -f "${SCRIPT_DIR}/Dockerfile" --push "${LIBRAE_ROOT}"

DIGEST="$(
    python3 -c \
        'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' \
        "${METADATA_FILE}"
)"
if [[ ! "${DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "Build did not return a valid image digest: ${DIGEST}" >&2
    exit 1
fi

echo "Published immutable trade image:"
echo "TRADE_IMAGE_REF=${IMAGE}@${DIGEST}"
echo "Set that value on the target, then run:"
echo "  cd deploy && ./trade.sh start <deployment_id> <account_id> <currency> <strategy> [sim|live] [poll_seconds]"
