#!/usr/bin/env python3
"""Run signal monitor once — smoke test / manual verification.

Usage:
    python scripts/run_monitor_once.py                          # ccxt adapter (default)
    python scripts/run_monitor_once.py --adapter binance_rest   # legacy REST fetcher
    python scripts/run_monitor_once.py --dry-run                # print only, no write

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
    parser.add_argument("--dry-run", action="store_true", help="Print result only, skip TimescaleDB write")
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
        print("[run_monitor_once] --dry-run: skipping TimescaleDB write")
        print(f"[line_protocol] {line}")
        return

    # Write to TimescaleDB
    try:
        from quant_lab.db.timescale_writer import get_conn, TIMESCALE_DSN

        now = datetime.now(timezone.utc)

        # Parse tags from line protocol
        tag_part = line.split(" ")[0] if " " in line else ""
        tag_map = {}
        for pair in tag_part.split(",")[1:]:
            if "=" in pair:
                k, v = pair.split("=", 1)
                tag_map[k] = v

        strategy = tag_map.get("strategy", "TrendPullback")
        symbol = args.symbol
        timeframe = args.timeframe
        run_id = "once"

        with get_conn(TIMESCALE_DSN) as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO backtest_runs
                   (run_id, strategy, symbol, timeframe, run_ts, mode)
                   VALUES (%s, %s, %s, %s, %s, 'sim')
                   ON CONFLICT (run_id) DO UPDATE SET run_ts = EXCLUDED.run_ts""",
                (run_id, strategy, symbol, timeframe, now),
            )

            price = float(field_map.get("price", "0").rstrip("i"))
            signal_strength = float(field_map.get("signal_strength", "0").rstrip("i"))
            confidence = float(field_map.get("confidence", "0").rstrip("i"))
            signal_type = tag_map.get("signal_type", "hold")

            cur.execute(
                """INSERT INTO strategy_signals
                   (ts, run_id, strategy, symbol, timeframe,
                    signal_type, source, price, signal_strength, confidence, quantity)
                   VALUES (%s, %s, %s, %s, %s, %s, 'manual', %s, %s, %s, 0)""",
                (now, run_id, strategy, symbol, timeframe,
                 signal_type, price, signal_strength, confidence),
            )
            cur.close()
        print(f"[run_monitor_once] ✅ Written to TimescaleDB (run_id={run_id})")
    except Exception as e:
        print(f"[run_monitor_once] ❌ TimescaleDB write failed: {e}")
        print("[run_monitor_once] Point was generated successfully but not persisted")


if __name__ == "__main__":
    main()
