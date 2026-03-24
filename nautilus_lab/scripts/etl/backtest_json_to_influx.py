#!/usr/bin/env python3
"""Load BacktestOutput JSON and write canonical measurements to InfluxDB.

Writes:
- strategy_signals (entry signal per trade)
- strategy_performance (canonical KPI)
- perf_equity_curve (strategy vs benchmark curve)
- trade_blotter
- trade_distribution
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from collections import Counter

from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nautilus_lab.backtest.persistence import load_backtest_output
from nautilus_lab.contracts import MEASUREMENT_SPECS
from nautilus_lab.monitoring.influx_writer import points_from_backtest


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Write backtest JSON to InfluxDB canonical measurements")
    p.add_argument("--input", required=True, help="Path to backtest JSON")
    p.add_argument("--sample", default="oos", help="sample tag (default: oos)")
    p.add_argument("--benchmark", default="TWSE", help="benchmark tag (default: TWSE)")
    p.add_argument("--dry-run", action="store_true", help="Build and print counts only")
    return p.parse_args()


def _validate_points_against_canonical(points: list) -> None:
    for p in points:
        spec = MEASUREMENT_SPECS.get(p._name)
        if spec is None:
            continue
        lp = p.to_line_protocol()
        tag_segment = lp.split(" ")[0]
        field_segment = lp.split(" ")[1] if " " in lp else ""

        for tag in [t.name for t in spec.tags if t.required]:
            if f",{tag}=" not in tag_segment and not tag_segment.startswith(f"{p._name},{tag}="):
                raise ValueError(f"{p._name} missing required tag: {tag}")

        for field in [f.name for f in spec.fields if f.required]:
            if f"{field}=" not in field_segment:
                raise ValueError(f"{p._name} missing required field: {field}")


def main() -> None:
    args = parse_args()
    path = Path(args.input)
    if not path.exists():
        raise FileNotFoundError(f"Input JSON not found: {path}")

    output = load_backtest_output(path)
    points = points_from_backtest(output, sample=args.sample, benchmark=args.benchmark)
    _validate_points_against_canonical(points)
    point_counts = Counter(p._name for p in points)

    if args.dry_run:
        print(f"[DRY-RUN] input={path}")
        print(f"[DRY-RUN] run_id={output.run_metadata.run_id}")
        print(f"[DRY-RUN] strategy={output.run_metadata.strategy}")
        print(f"[DRY-RUN] points={len(points)}")
        print(f"[DRY-RUN] counts={dict(point_counts)}")
        return

    url = os.getenv("INFLUX_URL", "http://localhost:8086")
    org = os.getenv("INFLUX_ORG", "quant_research")
    bucket = os.getenv("INFLUX_BUCKET", "nautilus_signals")
    token = os.getenv("INFLUX_TOKEN") or os.getenv("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN", "")
    if not token:
        raise RuntimeError("Missing INFLUX_TOKEN (or DOCKER_INFLUXDB_INIT_ADMIN_TOKEN)")

    with InfluxDBClient(url=url, token=token, org=org) as client:
        writer = client.write_api(write_options=SYNCHRONOUS)
        writer.write(bucket=bucket, org=org, record=points)

    print(
        f"[OK] wrote points={len(points)}, counts={dict(point_counts)}, run_id={output.run_metadata.run_id}, "
        f"strategy={output.run_metadata.strategy}, sample={args.sample}, benchmark={args.benchmark}, bucket={bucket}"
    )


if __name__ == "__main__":
    main()
