#!/usr/bin/env bash
# One-time VM bootstrap — installs Tailscale so the box has a private mesh
# IP before anything gets deployed. Run once per fresh VM, before
# cloud_deploy.sh. Independent of it on purpose: bootstrap (network access)
# and deploy (what runs once you're connected) are different concerns.
#
# Usage: ./deploy/bootstrap_tailscale.sh <user>@<host>
# TS_AUTHKEY can be set in .env to skip interactive auth; otherwise you'll
# get a URL printed to approve the machine in the Tailscale admin console.
set -euo pipefail

TARGET="${1:?Usage: bootstrap_tailscale.sh <user>@<host>}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${PROJECT_ROOT}/.env"
    set +a
fi

echo "[1/2] Installing Tailscale on ${TARGET}..."
ssh "${TARGET}" "curl -fsSL https://tailscale.com/install.sh | sh"

echo "[2/2] Bringing Tailscale up..."
if [[ -n "${TS_AUTHKEY:-}" ]]; then
    ssh "${TARGET}" "sudo tailscale up --authkey=${TS_AUTHKEY}"
else
    echo "No TS_AUTHKEY in .env — approve the machine via the URL below."
    ssh -t "${TARGET}" "sudo tailscale up"
fi

TS_IP="$(ssh "${TARGET}" "tailscale ip -4")"
echo ""
echo "Done. Tailscale IP: ${TS_IP}"
echo "Use it with cloud_deploy.sh: ./deploy/cloud_deploy.sh ${TARGET%%@*}@${TS_IP}"
