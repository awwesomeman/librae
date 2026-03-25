#!/usr/bin/env python3
"""Run SMA crossover backtest on real Binance BTC/USDT H1 data.

Skills: python, quant

Pipeline:
  1. Fetch BTC/USDT H1 OHLCV from Binance (6+ months)
  2. Run SMA crossover backtest
  3. Save BacktestOutput JSON
  4. Write canonical measurements to InfluxDB

Usage:
    python nautilus_lab/scripts/run_backtest_binance_btc.py [--months 6] [--dry-run] [--no-influx]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nautilus_lab.backtest.adapter import generate_run_id, metrics_dict_to_backtest_output
from nautilus_lab.backtest.persistence import save_backtest_output
from nautilus_lab.backtest.sample_data import run_simple_sma_crossover
from nautilus_lab.data.binance_fetcher import fetch_ohlcv
from nautilus_lab.monitoring.influx_writer import points_from_backtest


STRATEGY = "sma_crossover"
SYMBOL = "BTCUSDT"
TIMEFRAME = "H1"
DATA_SOURCE = "binance_spot"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest SMA crossover on real Binance BTC/USDT H1")
    p.add_argument("--months", type=int, default=6, help="Lookback months (default: 6)")
    p.add_argument("--fast", type=int, default=10, help="Fast SMA period (default: 10)")
    p.add_argument("--slow", type=int, default=30, help="Slow SMA period (default: 30)")
    p.add_argument("--out-dir", type=str, default=str(ROOT / "data" / "backtests"))
    p.add_argument("--no-influx", action="store_true", help="Skip InfluxDB write")
    p.add_argument("--dry-run", action="store_true", help="Print InfluxDB points without writing")
    p.add_argument("--sample", default="oos", help="Sample tag for InfluxDB (default: oos)")
    p.add_argument("--benchmark", default="BTC_BH", help="Benchmark tag (default: BTC_BH)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # 1) Fetch real data
    print(f"[1/4] Fetching Binance {SYMBOL} {TIMEFRAME} data ({args.months} months)...")
    df = fetch_ohlcv(symbol=SYMBOL, interval="1h", months=args.months)
    print(f"       bars={len(df)}, range={df['timestamp'].iloc[0]} ~ {df['timestamp'].iloc[-1]}")

    # 2) Backtest
    print(f"[2/4] Running SMA crossover (fast={args.fast}, slow={args.slow})...")
    metrics = run_simple_sma_crossover(df, fast_period=args.fast, slow_period=args.slow)

    start_str = df["timestamp"].iloc[0].isoformat()
    end_str = df["timestamp"].iloc[-1].isoformat()

    run_id = generate_run_id(STRATEGY, SYMBOL)
    output = metrics_dict_to_backtest_output(
        metrics,
        strategy=STRATEGY,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        start=start_str,
        end=end_str,
        data_source=DATA_SOURCE,
        run_id=run_id,
        sample=args.sample,
    )
    print(f"       trades={metrics['trades']}  sharpe={metrics.get('ann_sharpe', 0):.3f}  mdd={metrics.get('mdd', 0):.4f}")

    # 3) Save JSON
    out_dir = Path(args.out_dir)
    paths = save_backtest_output(output, out_dir)
    print(f"[3/4] Saved: {paths['json']}")

    # 4) InfluxDB
    if args.no_influx:
        print("[4/4] InfluxDB write skipped (--no-influx)")
        return

    points = points_from_backtest(output, sample=args.sample, benchmark=args.benchmark)
    counts = Counter(p._name for p in points)

    if args.dry_run:
        print(f"[4/4] [DRY-RUN] points={len(points)}, counts={dict(counts)}")
        return

    url = os.getenv("INFLUX_URL", "http://localhost:8086")
    org = os.getenv("INFLUX_ORG", "quant_research")
    bucket = os.getenv("INFLUX_BUCKET", "nautilus_signals")
    token = (
        os.getenv("INFLUX_TOKEN")
        or os.getenv("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN")
        or "change_me_super_secret_token"
    )

    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    with InfluxDBClient(url=url, token=token, org=org) as client:
        writer = client.write_api(write_options=SYNCHRONOUS)
        writer.write(bucket=bucket, org=org, record=points)

    print(f"[4/4] InfluxDB OK: points={len(points)}, counts={dict(counts)}, run_id={run_id}")


if __name__ == "__main__":
    main()
