#!/usr/bin/env python3
"""TrendPullback strategy runner.

Usage:
    python -m strategies.trendpullback.run --mode backtest --dry-run
    python -m strategies.trendpullback.run --mode backtest --symbol BTCUSDT --months 3
    python -m strategies.trendpullback.run --mode monitor   (future)
    python -m strategies.trendpullback.run --mode live      (future)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from librae import Backtest, compute_all
from librae.persistence import save_backtest_output
from db.timescale_writer import write_backtest_output
from librae.schema import (
    BacktestOutput, EquityCurvePoint, RunMetadata, StrategyMetrics, TradeRecord,
)
from librae.utils import generate_run_id

from .strategy import TrendPullbackStrategy
from .utils import fetch_and_prepare


def run_backtest(args: argparse.Namespace) -> None:
    """Run backtest mode."""
    print(f"[1/4] Fetching & preparing {args.symbol} ({args.months} months)...")
    df = fetch_and_prepare(args.symbol, args.months)
    print(f"       bars={len(df)}")

    print("[2/4] Running backtest...")
    strategy = TrendPullbackStrategy(max_hold_bars=args.max_hold_bars)
    bt = Backtest(data=df, strategy=strategy, market=args.market, initial_balance=args.initial_balance)
    result = bt.run()
    print(f"       trades={len(result.trades)}")

    print("[3/4] Computing metrics...")
    timeline = sorted(df.index.get_level_values("datetime").unique())
    start_ts = timeline[0].to_pydatetime()
    end_ts = timeline[-1].to_pydatetime()
    metrics = compute_all(result, start_ts, end_ts)
    print(f"       sharpe={metrics.sharpe:.3f}  mdd={metrics.max_drawdown:.4f}  ret={metrics.total_return:.4f}")

    if args.dry_run:
        print("[4/4] [DRY-RUN] Done.")
        return

    run_id = generate_run_id(f"trendpullback_{args.market}", args.symbol)
    output = _build_output(result, metrics, run_id, args.symbol, start_ts, end_ts, args.sample)
    out_dir = Path(args.out_dir)
    paths = save_backtest_output(output, out_dir)
    print(f"[4/4] Saved: {paths['json']}")

    if not args.no_db:
        try:
            counts = write_backtest_output(output)
            print(f"       DB: {counts}")
        except Exception as e:
            print(f"       DB write skipped: {e}")


def run_monitor(args: argparse.Namespace) -> None:
    """Run monitor mode (signal only, no orders). TODO: implement with LiveExecutor."""
    raise NotImplementedError("Monitor mode not yet implemented. Use experiments/ for now.")


def run_live(args: argparse.Namespace) -> None:
    """Run live mode (real orders). TODO: implement with LiveExecutor."""
    raise NotImplementedError("Live mode not yet implemented.")


def _build_output(result, metrics, run_id, symbol, start_ts, end_ts, sample):
    """Build canonical BacktestOutput from engine result."""
    now = datetime.now(tz=timezone.utc)
    run_metadata = RunMetadata(
        run_id=run_id, strategy="trendpullback", symbol=symbol,
        timeframe="H1", start_ts=start_ts, end_ts=end_ts,
        run_ts=now, data_source="binance", data_version="1", sample=sample,
    )
    trade_records = [
        TradeRecord(
            trade_id=f"{run_id}-t{i:04d}",
            entry_ts=t.entry_ts, exit_ts=t.exit_ts,
            symbol=symbol, side=t.side,
            entry_price=float(t.entry_price), exit_price=float(t.exit_price),
            quantity=float(t.quantity), price_unit="USDT", quantity_unit=symbol,
            gross_pnl=float(t.gross_pnl), net_pnl=float(t.net_pnl), pnl_unit="USDT",
            commission=float(t.commission), slippage=float(t.slippage),
            holding_bars=int(t.holding_bars),
        )
        for i, t in enumerate(result.trades)
    ]
    initial_eq = result.equity_curve[0].equity if result.equity_curve else 1.0
    equity_points = [
        EquityCurvePoint(
            ts=s.ts, equity=float(s.equity / initial_eq),
            equity_unit="index", ret_1d=0.0, drawdown=0.0,
        )
        for s in result.equity_curve
    ]
    return BacktestOutput(
        run_metadata=run_metadata, equity_curve=equity_points,
        trades=trade_records, metrics=metrics,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TrendPullback strategy runner")
    p.add_argument("--mode", default="backtest", choices=["backtest", "monitor", "live"])
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--market", default="crypto")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--initial-balance", type=float, default=100_000)
    p.add_argument("--max-hold-bars", type=int, default=24)
    p.add_argument("--sample", default="oos")
    p.add_argument("--out-dir", default="data/backtests")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-db", action="store_true", help="skip writing to TimescaleDB")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    dispatch = {"backtest": run_backtest, "monitor": run_monitor, "live": run_live}
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
