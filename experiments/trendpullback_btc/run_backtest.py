#!/usr/bin/env python3
"""Run backtest end-to-end: fetch data → ETL → strategy → engine → DB.

Usage:
    python scripts/run_backtest.py
    python scripts/run_backtest.py --months 3 --dry-run
    python scripts/run_backtest.py --instrument BTC_USDT --initial-balance 50000
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from librae import Backtest, BacktestResult, compute_all
from librae.config.market_config import get_market
from librae.persistence import save_backtest_output
from librae.archive import archive_backtest_parquet
from librae.schema import (
    BacktestOutput, EquityCurvePoint, RunMetadata, StrategyMetrics, TradeRecord,
)
from librae.utils import generate_run_id
from librae.data import fetch_ohlcv
from strategies.trendpullback.utils import (
    compute_entry_conditions,
    compute_exit_conditions,
    compute_features,
    compute_daily_gate,
    resample_to_daily,
)
from strategies.trendpullback.strategy import TrendPullbackStrategy


def prepare_data(symbol: str, months: int) -> pd.DataFrame:
    """Fetch OHLCV, compute features + signals, return MultiIndex DataFrame."""
    print(f"[1/5] Fetching {symbol} 1h data ({months} months)...")
    h1_raw = fetch_ohlcv(symbol=symbol, interval="1h", months=months)

    h1_base = h1_raw.set_index("timestamp")
    h1_base.index.name = "ts"

    # Compute features
    h1 = compute_features(h1_base)

    # Merge D1 daily gate
    d1 = compute_daily_gate(resample_to_daily(h1_base))
    h1 = _merge_daily_gate(h1, d1)

    # Compute entry/exit signals (pure boolean, no position tracking)
    h1["entry_signal"] = compute_entry_conditions(h1).values
    h1["exit_signal"] = compute_exit_conditions(h1).values

    print(f"       bars={len(h1)}, range={h1.index[0]} ~ {h1.index[-1]}")

    # Convert to MultiIndex (instrument, datetime)
    mi = pd.MultiIndex.from_arrays(
        [[symbol] * len(h1), h1.index], names=["instrument", "datetime"],
    )
    return h1.set_index(mi)


def _merge_daily_gate(h1: pd.DataFrame, d1: pd.DataFrame) -> pd.DataFrame:
    """Merge D1 daily_trend bool into H1 via merge_asof (no look-ahead)."""
    h1_feat = h1.copy()
    idx_name = h1_feat.index.name or "ts"

    d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
    d1_trend["daily_trend"] = (
        (d1_trend["close"] > d1_trend["ema20"])
        & (d1_trend["ema20"] > d1_trend["ema20_prev"])
    )

    d1_right = d1_trend[["daily_trend"]].copy()
    d1_right["d1_ts"] = d1_right.index
    d1_right = d1_right.reset_index(drop=True)

    h1_feat = pd.merge_asof(
        h1_feat.reset_index(),
        d1_right,
        left_on=idx_name,
        right_on="d1_ts",
        direction="backward",
    ).set_index(idx_name).drop(columns=["d1_ts"], errors="ignore")
    h1_feat["daily_trend"] = h1_feat["daily_trend"].fillna(False)

    return h1_feat


def build_output(
    result: BacktestResult,
    run_id: str,
    strategy_name: str,
    symbol: str,
    df: pd.DataFrame,
    sample: str | None = None,
) -> BacktestOutput:
    """Convert BacktestResult → canonical BacktestOutput for persistence."""
    timeline = sorted(df.index.get_level_values("datetime").unique())
    start_ts = timeline[0].to_pydatetime()
    end_ts = timeline[-1].to_pydatetime()
    now = datetime.now(tz=timezone.utc)

    quote_ccy = "USDT"  # TODO: derive from market config when multi-market

    run_metadata = RunMetadata(
        run_id=run_id,
        strategy=strategy_name,
        symbol=symbol,
        timeframe="H1",
        start_ts=start_ts,
        end_ts=end_ts,
        run_ts=now,
        data_source="binance",
        data_version="1",
        sample=sample,
    )

    trade_records = [
        TradeRecord(
            trade_id=f"{run_id}-t{i:04d}",
            entry_ts=t.entry_ts.to_pydatetime() if isinstance(t.entry_ts, pd.Timestamp) else t.entry_ts,
            exit_ts=t.exit_ts.to_pydatetime() if isinstance(t.exit_ts, pd.Timestamp) else t.exit_ts,
            symbol=symbol,
            side=t.side,
            entry_price=float(t.entry_price),
            exit_price=float(t.exit_price),
            quantity=float(t.quantity),
            price_unit=quote_ccy,
            quantity_unit=symbol,
            gross_pnl=float(t.gross_pnl),
            net_pnl=float(t.net_pnl),
            pnl_unit=quote_ccy,
            commission=float(t.commission),
            slippage=float(t.slippage),
            holding_bars=int(t.holding_bars),
        )
        for i, t in enumerate(result.trades)
    ]

    metrics = compute_all(result, start_ts, end_ts)

    # Build equity curve (simplified — no benchmark for now)
    initial_eq = result.equity_curve[0].equity if result.equity_curve else 1.0
    equity_points = [
        EquityCurvePoint(
            ts=s.ts.to_pydatetime() if isinstance(s.ts, pd.Timestamp) else s.ts,
            equity=float(s.equity / initial_eq),
            equity_unit="index",
            ret_1d=0.0,
            drawdown=0.0,
        )
        for s in result.equity_curve
    ]

    return BacktestOutput(
        run_metadata=run_metadata,
        equity_curve=equity_points,
        trades=trade_records,
        metrics=metrics,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run backtest via Librae engine")
    p.add_argument("--symbol", default="BTCUSDT", help="Trading symbol")
    p.add_argument("--market", default="crypto", help="Market key from markets.yaml")
    p.add_argument("--months", type=int, default=6, help="Lookback months")
    p.add_argument("--initial-balance", type=float, default=100_000, help="Starting cash")
    p.add_argument("--max-hold-bars", type=int, default=24, help="Max bars to hold")
    p.add_argument("--sample", default="oos", help="Sample tag")
    p.add_argument("--out-dir", default=str(ROOT / "data" / "backtests"))
    p.add_argument("--dry-run", action="store_true", help="Skip TimescaleDB write")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # 1) Fetch & prepare data
    df = prepare_data(args.symbol, args.months)

    # 2) Run backtest
    print("[2/5] Running backtest...")
    strategy = TrendPullbackStrategy(max_hold_bars=args.max_hold_bars)

    bt = Backtest(
        data=df,
        strategy=strategy,
        market=args.market,
        initial_balance=args.initial_balance,
    )
    result = bt.run()
    print(f"       trades={len(result.trades)}, equity_snapshots={len(result.equity_curve)}")

    # 3) Build output + metrics
    print("[3/5] Building BacktestOutput + metrics...")
    run_id = generate_run_id(f"trendpullback_{args.market}", args.symbol)
    output = build_output(result, run_id, "trendpullback", args.symbol, df, args.sample)

    m = output.metrics
    print(f"       trades={m.trades}  sharpe={m.sharpe:.3f}  "
          f"mdd={m.max_drawdown:.4f}  total_ret={m.total_return:.4f}")

    # 4) Save JSON + Parquet
    print("[4/5] Saving outputs...")
    out_dir = Path(args.out_dir)
    paths = save_backtest_output(output, out_dir)
    print(f"       JSON: {paths['json']}")

    # 5) Write to TimescaleDB
    if args.dry_run:
        print("[5/5] [DRY-RUN] Skipping TimescaleDB write")
        return

    from db.timescale_writer import write_backtest_output as ts_write
    ts_counts = ts_write(output)
    print(f"[5/5] TimescaleDB written: {ts_counts}")
    print(f"       run_id={run_id}")


if __name__ == "__main__":
    main()
