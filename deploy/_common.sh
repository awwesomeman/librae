#!/usr/bin/env bash
# Shared helpers for sim scripts.

# Derive container name from strategy config.
# Usage: CONTAINER=$(sim_container_name <strategy>)
sim_container_name() {
    local strategy="$1"
    local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local symbol
    symbol=$(grep 'symbol:' "${script_dir}/../strategies/${strategy}/config.yaml" | head -1 | awk '{print $2}' | tr '[:upper:]' '[:lower:]')
    echo "quant_sim_${strategy}_${symbol}"
}
