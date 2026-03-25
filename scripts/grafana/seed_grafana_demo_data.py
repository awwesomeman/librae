"""Seed InfluxDB with demo data for Grafana dashboard testing.

Generates realistic time-series data for three measurements:
  - strategy_signals: signal events with strength, confidence, price
  - strategy_performance: equity, pnl, drawdown, win_rate snapshots
  - strategy_health: heartbeat events, error counts, data latency

Usage:
    python scripts/grafana/seed_grafana_demo_data.py \
        --url http://localhost:8086 \
        --token YOUR_INFLUXDB_TOKEN \
        --org nautilus \
        --bucket nautilus

    # Dry-run (print line protocol to stdout):
    python scripts/grafana/seed_grafana_demo_data.py --dry-run
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "1.0.0"
STRATEGIES = ["momentum_btc", "mean_revert_eth"]
ASSETS = ["BTCUSDT", "ETHUSDT"]
TIMEFRAMES = ["H1", "H4"]
SIGNAL_TYPES = ["entry", "exit", "scale_in"]
SIDES = ["buy", "sell"]
STRATEGY_BASE_PRICE = {"momentum_btc": 65000.0, "mean_revert_eth": 3200.0}
RUN_ID = "demo_run_001"
SAMPLE = "oos"
BENCHMARK = "primary"

DAYS_BACK = 7
POINTS_PER_HOUR = 12  # one point every 5 minutes


def _ts_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


def _escape_tag(v: str) -> str:
    return v.replace(" ", r"\ ").replace(",", r"\,").replace("=", r"\=")


def generate_signals(now: datetime) -> list[str]:
    """Generate strategy_signals measurement points."""
    lines: list[str] = []
    start = now - timedelta(days=DAYS_BACK)
    total_points = DAYS_BACK * 24 * POINTS_PER_HOUR

    for strategy, asset in zip(STRATEGIES, ASSETS):
        base_price = STRATEGY_BASE_PRICE[strategy]
        for tf in TIMEFRAMES:
            tag_prefix = (
                f"strategy={_escape_tag(strategy)},"
                f"asset={_escape_tag(asset)},"
                f"timeframe={_escape_tag(tf)},"
            )
            tag_suffix = (
                f"run_id={_escape_tag(RUN_ID)},"
                f"sample={_escape_tag(SAMPLE)},"
                f"schema_version={_escape_tag(SCHEMA_VERSION)}"
            )
            for i in range(total_points):
                if random.random() < 0.4:
                    continue

                dt = start + timedelta(minutes=i * 5)
                signal_type = random.choice(SIGNAL_TYPES)
                side = random.choice(SIDES)
                strength = max(0.0, min(1.0, 0.5 + 0.3 * math.sin(i / 200) + random.gauss(0, 0.15)))
                confidence = max(0.0, min(1.0, strength * 0.8 + random.gauss(0, 0.1)))
                price = base_price * (1 + 0.02 * math.sin(i / 100) + random.gauss(0, 0.005))
                quantity = round(random.uniform(0.01, 0.5), 4)

                tags = (
                    f"{tag_prefix}"
                    f"signal_type={_escape_tag(signal_type)},"
                    f"side={_escape_tag(side)},"
                    f"{tag_suffix}"
                )
                fields = (
                    f"signal_strength={strength:.6f},"
                    f"confidence={confidence:.6f},"
                    f"price={price:.2f},"
                    f"quantity={quantity}"
                )
                lines.append(f"strategy_signals,{tags} {fields} {_ts_ns(dt)}")
    return lines


def generate_performance(now: datetime) -> list[str]:
    """Generate strategy_performance measurement points (equity, pnl, drawdown, win_rate)."""
    lines: list[str] = []
    start = now - timedelta(days=DAYS_BACK)
    total_points = DAYS_BACK * 24 * POINTS_PER_HOUR

    for strategy, asset in zip(STRATEGIES, ASSETS):
        for tf in TIMEFRAMES:
            equity = 100000.0
            peak_equity = equity
            wins = 0
            total_trades = 0

            for i in range(total_points):
                dt = start + timedelta(minutes=i * 5)

                # Simulate equity changes
                pnl = random.gauss(5, 80) + 20 * math.sin(i / 300)
                equity += pnl
                peak_equity = max(peak_equity, equity)
                drawdown_pct = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
                drawdown_duration_hours = 0.0
                if drawdown_pct < -0.001:
                    drawdown_duration_hours = min(168.0, i * 5 / 60 * abs(drawdown_pct) * 10)

                # Simulate rolling win rate
                if random.random() < 0.1:
                    total_trades += 1
                    if random.random() < 0.52:
                        wins += 1
                win_rate = wins / total_trades if total_trades > 0 else 0.5

                tags = (
                    f"strategy={_escape_tag(strategy)},"
                    f"asset={_escape_tag(asset)},"
                    f"timeframe={_escape_tag(tf)},"
                    f"benchmark={_escape_tag(BENCHMARK)},"
                    f"run_id={_escape_tag(RUN_ID)},"
                    f"sample={_escape_tag(SAMPLE)},"
                    f"schema_version={_escape_tag(SCHEMA_VERSION)}"
                )
                fields = (
                    f"equity={equity:.2f},"
                    f"pnl={pnl:.2f},"
                    f"drawdown_pct={drawdown_pct:.6f},"
                    f"drawdown_duration_hours={drawdown_duration_hours:.2f},"
                    f"win_rate={win_rate:.6f}"
                )
                lines.append(f"strategy_performance,{tags} {fields} {_ts_ns(dt)}")
    return lines


def generate_health(now: datetime) -> list[str]:
    """Generate strategy_health measurement points (heartbeat, errors, latency)."""
    lines: list[str] = []
    start = now - timedelta(days=DAYS_BACK)
    total_points = DAYS_BACK * 24 * POINTS_PER_HOUR

    for strategy in STRATEGIES:
        for i in range(total_points):
            dt = start + timedelta(minutes=i * 5)

            event_count = random.randint(5, 30)
            error_count = 0 if random.random() < 0.95 else random.randint(1, 3)
            latency_ms = max(10.0, random.gauss(120, 50) + 30 * math.sin(i / 100))

            tags = (
                f"strategy={_escape_tag(strategy)},"
                f"schema_version={_escape_tag(SCHEMA_VERSION)}"
            )
            fields = (
                f"event_count={event_count}i,"
                f"error_count={error_count}i,"
                f"data_latency_ms={latency_ms:.2f}"
            )
            lines.append(f"strategy_health,{tags} {fields} {_ts_ns(dt)}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed InfluxDB with Grafana demo data")
    parser.add_argument("--url", default="http://localhost:8086", help="InfluxDB URL")
    parser.add_argument("--token", default="", help="InfluxDB API token")
    parser.add_argument("--org", default="nautilus", help="InfluxDB organization")
    parser.add_argument("--bucket", default="nautilus", help="InfluxDB bucket")
    parser.add_argument("--dry-run", action="store_true", help="Print line protocol to stdout")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)
    now = datetime.now(tz=timezone.utc)

    print(f"Generating demo data ({DAYS_BACK} days, {len(STRATEGIES)} strategies)...")
    all_lines = generate_signals(now) + generate_performance(now) + generate_health(now)
    print(f"Generated {len(all_lines)} data points.")

    if args.dry_run:
        for line in all_lines[:50]:
            print(line)
        print(f"... ({len(all_lines) - 50} more lines)")
        return

    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
    except ImportError:
        print("ERROR: influxdb-client not installed. Run: pip install influxdb-client", file=sys.stderr)
        print("Or use --dry-run to preview line protocol output.", file=sys.stderr)
        sys.exit(1)

    client = InfluxDBClient(url=args.url, token=args.token, org=args.org)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    batch_size = 5000
    for i in range(0, len(all_lines), batch_size):
        batch = all_lines[i : i + batch_size]
        write_api.write(bucket=args.bucket, record=batch)
        print(f"  Written {min(i + batch_size, len(all_lines))}/{len(all_lines)} points...")

    client.close()
    print("Done! Data seeded successfully.")


if __name__ == "__main__":
    main()
