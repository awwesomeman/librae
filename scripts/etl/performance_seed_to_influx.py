#!/usr/bin/env python3
"""Seed L1 performance demo data to InfluxDB.

Writes two measurements:
- strategy_performance: run-level aggregate KPIs
- perf_equity_curve: timestamped equity curve

Required L1 contract fields included:
- run_id
- sample (train/oos)
- benchmark
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision

from quant_lab.contracts import SCHEMA_VERSION


@dataclass
class PerfRow:
    sample: str
    total_return: float
    annual_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    trades: int


def _sample_rows() -> list[PerfRow]:
    return [
        PerfRow("train", 0.186, 0.214, 1.35, -0.092, 0.56, 182),
        PerfRow("oos", 0.073, 0.101, 0.91, -0.118, 0.52, 67),
    ]


def build_performance_points(strategy: str, symbol: str, timeframe: str, run_id: str, benchmark: str) -> list[Point]:
    now = datetime.now(timezone.utc)
    points: list[Point] = []
    for row in _sample_rows():
        p = (
            Point("strategy_performance")
            .tag("schema_version", SCHEMA_VERSION)
            .tag("strategy", strategy)
            .tag("symbol", symbol)
            .tag("timeframe", timeframe)
            .tag("run_id", run_id)
            .tag("sample", row.sample)
            .tag("benchmark", benchmark)
            .field("total_return", float(row.total_return))
            .field("annual_return", float(row.annual_return))
            .field("sharpe", float(row.sharpe))
            .field("max_drawdown", float(row.max_drawdown))
            .field("win_rate", float(row.win_rate))
            .field("trades", int(row.trades))
            .time(now, WritePrecision.NS)
        )
        points.append(p)
    return points


def build_equity_curve_points(strategy: str, symbol: str, timeframe: str, run_id: str, benchmark: str) -> list[Point]:
    start = datetime.now(timezone.utc) - timedelta(days=10)
    train_curve = [1.000, 1.008, 1.017, 1.011, 1.024, 1.039, 1.031, 1.048, 1.057, 1.071]
    oos_curve = [1.071, 1.066, 1.074, 1.069, 1.081, 1.077, 1.089, 1.083, 1.096, 1.104]
    benchmark_curve = [1.000, 1.002, 1.006, 1.001, 1.009, 1.013, 1.011, 1.017, 1.019, 1.025,
                       1.026, 1.022, 1.028, 1.024, 1.031, 1.030, 1.036, 1.033, 1.041, 1.046]

    strategy_curve = train_curve + oos_curve
    points: list[Point] = []
    peak = strategy_curve[0]

    for i, equity in enumerate(strategy_curve):
        ts = start + timedelta(days=i)
        sample = "train" if i < len(train_curve) else "oos"
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        ret_1d = 0.0 if i == 0 else (equity / strategy_curve[i - 1] - 1.0)

        bp = benchmark_curve[i]
        bp_prev = benchmark_curve[i - 1] if i > 0 else benchmark_curve[0]
        bp_ret_1d = 0.0 if i == 0 else (bp / bp_prev - 1.0)

        p = (
            Point("perf_equity_curve")
            .tag("schema_version", SCHEMA_VERSION)
            .tag("strategy", strategy)
            .tag("symbol", symbol)
            .tag("timeframe", timeframe)
            .tag("run_id", run_id)
            .tag("sample", sample)
            .tag("benchmark", benchmark)
            .field("equity", float(equity))
            .field("ret_1d", float(ret_1d))
            .field("drawdown", float(drawdown))
            .field("benchmark_equity", float(bp))
            .field("benchmark_ret_1d", float(bp_ret_1d))
            .time(ts, WritePrecision.NS)
        )
        points.append(p)

    return points


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed L1 strategy performance data to InfluxDB")
    parser.add_argument("--strategy", default="DemoBreakout_v1.0")
    parser.add_argument("--symbol", default="TXF")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--run-id", default=f"seed-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    parser.add_argument("--benchmark", default="TWSE")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    perf_points = build_performance_points(
        strategy=args.strategy,
        symbol=args.symbol,
        timeframe=args.timeframe,
        run_id=args.run_id,
        benchmark=args.benchmark,
    )
    curve_points = build_equity_curve_points(
        strategy=args.strategy,
        symbol=args.symbol,
        timeframe=args.timeframe,
        run_id=args.run_id,
        benchmark=args.benchmark,
    )

    if args.dry_run:
        print(f"[DRY-RUN] strategy_performance points={len(perf_points)}")
        print(f"[DRY-RUN] perf_equity_curve points={len(curve_points)}")
        print(f"[DRY-RUN] run_id={args.run_id}, benchmark={args.benchmark}")
        return

    url = os.getenv("INFLUX_URL", "http://localhost:8086")
    org = os.getenv("INFLUX_ORG", "quant_research")
    bucket = os.getenv("INFLUX_BUCKET", "nautilus_signals")
    token = os.getenv("INFLUX_TOKEN")

    if not token:
        raise RuntimeError("Missing INFLUX_TOKEN")

    with InfluxDBClient(url=url, token=token, org=org) as client:
        write_api = client.write_api()
        write_api.write(bucket=bucket, org=org, record=perf_points)
        write_api.write(bucket=bucket, org=org, record=curve_points)

    print(
        f"[OK] wrote strategy_performance={len(perf_points)}, "
        f"perf_equity_curve={len(curve_points)} points, run_id={args.run_id}, bucket={bucket}"
    )


if __name__ == "__main__":
    main()
