#!/usr/bin/env bash
# Shared helpers for sim scripts.

# Derive container name from strategy config.
# Usage: CONTAINER=$(sim_container_name <strategy>)
sim_container_name() {
    local strategy="$1"
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local config="${script_dir}/../strategies/${strategy}/config.yaml"
    # On a no-repo VM there's no strategies/ tree to read symbol from —
    # fall back to strategy name alone (still unique per running strategy).
    if [[ ! -f "${config}" ]]; then
        echo "quant_sim_${strategy}"
        return
    fi
    local symbol
    symbol=$(grep 'symbol:' "${config}" | head -1 | awk '{print $2}' | tr '[:upper:]' '[:lower:]')
    echo "quant_sim_${strategy}_${symbol}"
}
