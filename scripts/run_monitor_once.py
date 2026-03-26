#!/usr/bin/env python3
"""Run signal monitor once — smoke test / manual verification.

Usage:
    python scripts/run_monitor_once.py                          # ccxt adapter (default)
    python scripts/run_monitor_once.py --adapter binance_rest   # legacy REST fetcher
    python scripts/run_monitor_once.py --dry-run                # print only, no write

Environment variables (only needed without --dry-run):
    INFLUX_URL     (default: http://localhost:8086)
    INFLUX_TOKEN
    INFLUX_ORG     (default: quant_lab)
    INFLUX_BUCKET  (default: quant_signals)

CCXT credentials (optional, read-only mode when absent):
    CCXT_API_KEY
    CCXT_API_SECRET
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root on path
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import pandas as pd
from quant_lab.monitoring.signal_monitor import run_monitor


class BinanceLiveAdapter:
    """Adapter bridging binance_fetcher into the OHLCVAdapter protocol."""

    _TF_MAP = {"1h": "1h", "4h": "4h", "1d": "1d"}

    def __init__(self, use_cache: bool = True):
        self._use_cache = use_cache

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        from quant_lab.data.binance_fetcher import fetch_ohlcv as _fetch

        raw_symbol = symbol.replace("/", "")
        interval = self._TF_MAP.get(timeframe, timeframe)
        df = _fetch(
            symbol=raw_symbol,
            interval=interval,
            months=2,
            use_cache=self._use_cache,
        )
        if "timestamp" in df.columns and "ts" not in df.columns:
            df = df.rename(columns={"timestamp": "ts"})
        return df.tail(limit).reset_index(drop=True)


def _build_adapter(name: str):
    """Return the appropriate adapter instance."""
    if name == "ccxt":
        from quant_lab.adapters.crypto_adapter import CryptoAdapter

        api_key = os.environ.get("CCXT_API_KEY", "")
        api_secret = os.environ.get("CCXT_API_SECRET", "")
        mode = "read-only" if not api_key else "authenticated"
        print(f"[run_monitor_once] Using CryptoAdapter (ccxt/binance, {mode})")
        return CryptoAdapter(
            exchange_id="binance",
            api_key=api_key,
            api_secret=api_secret,
        )
    elif name == "binance_rest":
        print("[run_monitor_once] Using legacy BinanceLiveAdapter (REST)")
        return BinanceLiveAdapter(use_cache=True)
    else:
        raise ValueError(f"Unknown adapter: {name}. Use 'ccxt' or 'binance_rest'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run signal monitor once")
    parser.add_argument("--dry-run", action="store_true", help="Print result only, skip InfluxDB write")
    parser.add_argument("--adapter", default="ccxt", choices=["ccxt", "binance_rest"],
                        help="Data adapter to use (default: ccxt)")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair (default: BTC/USDT)")
    parser.add_argument("--timeframe", default="1h", help="Candle interval (default: 1h)")
    args = parser.parse_args()

    print(f"[run_monitor_once] symbol={args.symbol} timeframe={args.timeframe} "
          f"adapter={args.adapter} dry_run={args.dry_run}")

    adapter = _build_adapter(args.adapter)
    pt = run_monitor(adapter, symbol=args.symbol, timeframe=args.timeframe, source="manual", run_id="once")

    if pt is None:
        print("[run_monitor_once] No data returned — point is None")
        return

    # Parse line protocol for summary
    line = pt.to_line_protocol()
    fields_str = line.split(" ")[1]
    field_map = {}
    for pair in fields_str.split(","):
        k, v = pair.split("=", 1)
        field_map[k] = v

    ts_ns = int(line.split(" ")[2])
    ts_dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)

    print(
        f"signal={field_map.get('signal_strength', '?')}, "
        f"confidence={field_map.get('confidence', '?')}, "
        f"price={field_map.get('price', '?')}, "
        f"ts={ts_dt.isoformat()}"
    )

    if args.dry_run:
        print("[run_monitor_once] --dry-run: skipping InfluxDB write")
        print(f"[line_protocol] {line}")
        return

    # Write to InfluxDB
    influx_url = os.environ.get("INFLUX_URL", "http://localhost:8086")
    influx_token = os.environ.get("INFLUX_TOKEN", "")
    influx_org = os.environ.get("INFLUX_ORG", "quant_lab")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "quant_signals")

    if not influx_token:
        print("[run_monitor_once] WARNING: INFLUX_TOKEN not set, skipping write")
        return

    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=influx_bucket, org=influx_org, record=pt)
        print(f"[run_monitor_once] ✅ Written to InfluxDB ({influx_url}, bucket={influx_bucket})")
        client.close()
    except Exception as e:
        print(f"[run_monitor_once] ❌ InfluxDB write failed: {e}")
        print("[run_monitor_once] Point was generated successfully but not persisted")


if __name__ == "__main__":
    main()
