#!/usr/bin/env python3
"""Load strategy signals from JSONL and write to InfluxDB.

Default input: data/monitor/signals.jsonl
Measurement: strategy_signals

Environment variables:
- INFLUX_URL (default: http://localhost:8086)
- INFLUX_ORG (default: quant_research)
- INFLUX_BUCKET (default: nautilus_signals)
- INFLUX_TOKEN (required)
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from influxdb_client import InfluxDBClient, Point, WritePrecision

from nautilus_lab.contracts import (
    REQUIRED_SIGNAL_KEYS,
    SCHEMA_VERSION,
    parse_utc_timestamp,
    require_keys,
)


MEASUREMENT = "strategy_signals"
VALID_SIDES = {"buy", "sell", "hold"}


@dataclass
class Stats:
    total_lines: int = 0
    parsed_lines: int = 0
    written_points: int = 0
    skipped_lines: int = 0
    build_error_lines: int = 0
    write_errors: int = 0


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_side(raw: Any) -> str:
    side = str(raw or "").strip().lower()
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side={raw!r}, expected one of {sorted(VALID_SIDES)}")
    return side


def _build_point(record: dict[str, Any]) -> Point:
    require_keys(record, REQUIRED_SIGNAL_KEYS, "strategy_signals record")

    strategy = str(record["strategy"])
    symbol = str(record["symbol"])
    timeframe = str(record["timeframe"])
    source = str(record.get("source", "jsonl"))
    run_id = str(record.get("run_id", "na"))
    signal_type = str(record.get("signal_type", "entry"))

    side = _normalize_side(record["side"])
    signal_strength = _to_float(record.get("signal_strength"))
    confidence = _to_float(record.get("confidence"))
    price = _to_float(record.get("price"))
    quantity = _to_float(record.get("quantity"))

    if signal_strength is None:
        signal_strength = {"buy": 1.0, "sell": -1.0, "hold": 0.0}[side]

    point = (
        Point(MEASUREMENT)
        .tag("schema_version", SCHEMA_VERSION)
        .tag("strategy", strategy)
        .tag("symbol", symbol)
        .tag("timeframe", timeframe)
        .tag("side", side)
        .tag("source", source)
        .tag("run_id", run_id)
        .tag("signal_type", signal_type)
        .field("signal_strength", float(signal_strength))
    )

    if confidence is not None:
        point.field("confidence", float(confidence))
    if price is not None:
        point.field("price", float(price))
    if quantity is not None:
        point.field("quantity", float(quantity))

    timestamp = parse_utc_timestamp(record["timestamp"])
    point.time(timestamp, WritePrecision.NS)
    return point


def _iter_points(input_path: Path, stats: Stats) -> Iterable[Point]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    with input_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            stats.total_lines += 1
            raw = line.strip()
            if not raw:
                stats.skipped_lines += 1
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                stats.skipped_lines += 1
                print(f"[WARN] line {line_no}: invalid JSON ({exc})")
                continue
            if not isinstance(payload, dict):
                stats.skipped_lines += 1
                print(f"[WARN] line {line_no}: JSON must be object, got {type(payload).__name__}")
                continue

            stats.parsed_lines += 1
            try:
                yield _build_point(payload)
            except Exception as exc:  # pragma: no cover - defensive ETL guard
                stats.build_error_lines += 1
                print(f"[WARN] line {line_no}: cannot build point ({exc})")


def _chunks(points: Iterable[Point], size: int) -> Iterable[list[Point]]:
    bucket: list[Point] = []
    for p in points:
        bucket.append(p)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


def main() -> None:
    parser = argparse.ArgumentParser(description="Write strategy signals JSONL to InfluxDB")
    parser.add_argument("--input", default="data/monitor/signals.jsonl", help="Path to JSONL file")
    parser.add_argument("--batch-size", type=int, default=500, help="Influx write batch size")
    parser.add_argument("--dry-run", action="store_true", help="Parse input only, do not write")
    args = parser.parse_args()

    batch_size = max(1, int(args.batch_size))
    input_path = Path(args.input)
    stats = Stats()
    point_iter = _iter_points(input_path, stats)

    if args.dry_run:
        parsed = sum(len(chunk) for chunk in _chunks(point_iter, batch_size))
        print(f"[DRY-RUN] parsed points: {parsed}")
        print(
            f"[INFO] stats: total={stats.total_lines}, parsed={stats.parsed_lines}, "
            f"skipped={stats.skipped_lines}, build_errors={stats.build_error_lines}, "
            "written=0"
        )
        return

    url = os.getenv("INFLUX_URL", "http://localhost:8086")
    org = os.getenv("INFLUX_ORG", "quant_research")
    bucket = os.getenv("INFLUX_BUCKET", "nautilus_signals")
    token = os.getenv("INFLUX_TOKEN")

    if not token:
        raise RuntimeError("Missing INFLUX_TOKEN")

    with InfluxDBClient(url=url, token=token, org=org) as client:
        write_api = client.write_api()
        for idx, chunk in enumerate(_chunks(point_iter, batch_size), start=1):
            try:
                write_api.write(bucket=bucket, org=org, record=chunk)
                stats.written_points += len(chunk)
            except Exception as exc:  # pragma: no cover - runtime safety
                stats.write_errors += 1
                print(f"[WARN] batch {idx}: write failed ({exc})")

    if stats.written_points == 0:
        print(f"[WARN] no points written to measurement={MEASUREMENT}")
    else:
        print(
            f"[OK] wrote {stats.written_points} points to measurement={MEASUREMENT}, "
            f"bucket={bucket}, org={org}"
        )

    print(
        f"[INFO] stats: total={stats.total_lines}, parsed={stats.parsed_lines}, "
        f"skipped={stats.skipped_lines}, build_errors={stats.build_error_lines}, "
        f"write_errors={stats.write_errors}, written={stats.written_points}"
    )


if __name__ == "__main__":
    main()
