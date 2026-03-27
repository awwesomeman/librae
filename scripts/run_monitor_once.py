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
from monitoring.signal_monitor import run_monitor


class BinanceLiveAdapter:
    """Adapter bridging binance_fetcher into the OHLCVAdapter protocol."""

    _TF_MAP = {"1h": "1h", "4h": "4h", "1d": "1d"}

    def __init__(self, use_cache: bool = True):
        self._use_cache = use_cache

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        from pipeline.fetchers.binance_fetcher import fetch_ohlcv as _fetch

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
        from brokers.crypto_adapter import CryptoAdapter

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
    result = run_monitor(adapter, symbol=args.symbol, timeframe=args.timeframe, source="manual", run_id="once")

    if result is None:
        print("[run_monitor_once] No data returned — result is None")
        return

    print(
        f"signal={result.signal}, "
        f"confidence={result.confidence}, "
        f"price={result.price}, "
        f"ts={result.ts.isoformat()}"
    )

    if args.dry_run:
        print("[run_monitor_once] --dry-run: skipping TimescaleDB write")
        print(f"[summary] signal_type={result.signal_type} strategy={result.strategy} "
              f"symbol={result.symbol} timeframe={result.timeframe}")
        return

    # Write to TimescaleDB
    try:
        from db.timescale_writer import get_conn, TIMESCALE_DSN

        now = datetime.now(timezone.utc)
        run_id = "once"

        with get_conn(TIMESCALE_DSN) as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO backtest_runs
                   (run_id, strategy, symbol, timeframe, run_ts, mode)
                   VALUES (%s, %s, %s, %s, %s, 'sim')
                   ON CONFLICT (run_id) DO UPDATE SET run_ts = EXCLUDED.run_ts""",
                (run_id, result.strategy, result.symbol, result.timeframe, now),
            )

            cur.execute(
                """INSERT INTO strategy_signals
                   (ts, run_id, strategy, symbol, timeframe,
                    signal_type, source, price, signal_strength, confidence, quantity)
                   VALUES (%s, %s, %s, %s, %s, %s, 'manual', %s, %s, %s, 0)""",
                (now, run_id, result.strategy, result.symbol, result.timeframe,
                 result.signal_type, result.price, result.signal_strength, result.confidence),
            )
            cur.close()
        print(f"[run_monitor_once] ✅ Written to TimescaleDB (run_id={run_id})")
    except Exception as e:
        print(f"[run_monitor_once] ❌ TimescaleDB write failed: {e}")
        print("[run_monitor_once] Result was generated successfully but not persisted")


if __name__ == "__main__":
    main()
