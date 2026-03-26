#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   INFLUX_TOKEN=xxx scripts/etl/replay_l1_seed.sh
# Optional env:
#   INFLUX_URL, INFLUX_ORG, INFLUX_BUCKET

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

RUN_ID="l1-seed-$(date -u +%Y%m%d%H%M%S)"

echo "[1/2] writing strategy_signals from data/monitor/signals.jsonl"
"$PY" scripts/etl/signals_to_influx.py \
  --input data/monitor/signals.jsonl

echo "[2/2] writing strategy_performance + perf_equity_curve (run_id=$RUN_ID)"
"$PY" scripts/etl/performance_seed_to_influx.py \
  --strategy "DemoBreakout_v1.0-H1-LS-TXF" \
  --symbol "TXF" \
  --timeframe "H1" \
  --benchmark "TWSE" \
  --run-id "$RUN_ID"

echo "[OK] L1 replay completed (run_id=$RUN_ID)"
