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
from db.timescale_writer import write_backtest_output, write_ohlcv
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
    bt.set_benchmark("auto")
    result = bt.run()
    print(f"       trades={len(result.trades)}")

    print("[3/4] Computing metrics...")
    timeline = sorted(df.index.get_level_values("datetime").unique())
    start_ts = timeline[0].to_pydatetime()
    end_ts = timeline[-1].to_pydatetime()
    metrics = compute_all(result, start_ts, end_ts, annualize=not args.no_annualize)
    sharpe_str = f"{metrics.sharpe:.3f}" if metrics.sharpe is not None else "N/A"
    print(f"       sharpe={sharpe_str}  mdd={metrics.max_drawdown:.4f}  ret={metrics.total_return:.4f}")

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
            ohlcv_df = df.droplevel("instrument")[["open", "high", "low", "close", "volume"]]
            ohlcv_df.index.name = "ts"
            ohlcv_count = write_ohlcv(ohlcv_df, args.symbol, "H1", run_id)
            counts["ohlcv"] = ohlcv_count
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
        run_ts=now, data_source="binance", mode="backtest", sample=sample,
    )
    trade_records = [
        TradeRecord(
            trade_id=f"{run_id}-t{i:04d}",
            entry_ts=t.entry_ts, exit_ts=t.exit_ts,
            symbol=symbol, side=t.side,
            entry_price=float(t.entry_price), exit_price=float(t.exit_price),
            quantity=float(t.quantity),
            gross_pnl=float(t.gross_pnl), net_pnl=float(t.net_pnl),
            commission=float(t.commission), slippage=float(t.slippage),
            holding_bars=int(t.holding_bars),
        )
        for i, t in enumerate(result.trades)
    ]

    # Compute equity, drawdown, benchmark in single pass
    # WHY: equity stored as actual value (initial_balance based), not normalized,
    # so Grafana Y-axis shows real dollar amounts aligned with initial_balance.
    has_benchmark = result.benchmark_curve is not None and len(result.benchmark_curve) > 0
    equity_points = []
    peak = 0.0
    prev_eq = result.equity_curve[0].equity if result.equity_curve else 1.0
    prev_bm = 1.0
    for i, s in enumerate(result.equity_curve):
        eq = s.equity
        peak = max(peak, eq)
        drawdown = (eq - peak) / peak if peak > 0 else 0.0
        ret_1d = (eq / prev_eq - 1.0) if prev_eq > 0 else 0.0
        prev_eq = eq

        bm_eq = None
        bm_ret = None
        if has_benchmark and i < len(result.benchmark_curve):
            bm_eq = float(result.benchmark_curve[i])
            bm_ret = (bm_eq / prev_bm - 1.0) if prev_bm > 0 else 0.0
            prev_bm = bm_eq

        equity_points.append(EquityCurvePoint(
            ts=s.ts, equity=float(eq),
            ret_1d=float(ret_1d), drawdown=float(drawdown),
            benchmark_equity=bm_eq, benchmark_ret_1d=bm_ret,
        ))

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
    p.add_argument("--no-annualize", action="store_true",
                   help="skip annualized metrics (sharpe, sortino, calmar, CAGR)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-db", action="store_true", help="skip writing to TimescaleDB")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    dispatch = {"backtest": run_backtest, "monitor": run_monitor, "live": run_live}
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
