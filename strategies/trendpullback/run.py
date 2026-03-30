#!/usr/bin/env python3
"""TrendPullback strategy runner.

Usage:
    python -m strategies.trendpullback.run --mode backtest --dry-run
    python -m strategies.trendpullback.run --mode backtest --symbol BTCUSDT --months 3
    python -m strategies.trendpullback.run --mode sim
    python -m strategies.trendpullback.run --mode live      (future)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

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


def run_sim(args: argparse.Namespace) -> None:
    """Run sim mode — paper trading with live data, Telegram alerts, DB writes."""
    import logging as _logging

    from brokers.crypto_adapter import CryptoAdapter
    from librae.config.market_config import get_market
    from librae.cost_model import CostModel
    from librae.live_executor import LiveExecutor
    from librae.live_runner import LiveRunner
    from librae.notifications.telegram import TelegramAdapter
    from librae.strategy import Action
    from db.timescale_writer import (
        update_heartbeat, write_equity_point, write_ohlcv, write_performance,
        write_run_metadata, write_signal, write_trade,
    )

    from .utils import prepare_signals

    _logger = _logging.getLogger(__name__)
    symbols = [s.strip() for s in args.symbol.split(",")]
    adapter = CryptoAdapter()
    telegram = TelegramAdapter()
    cost_model = CostModel.from_market(get_market(args.market))
    run_id = generate_run_id(f"trendpullback_{args.market}", symbols[0])

    # Register run in DB so Grafana mode='sim' dropdown picks it up
    if not args.no_db:
        try:
            now = datetime.now(tz=timezone.utc)
            write_run_metadata(
                run_id=run_id, strategy="trendpullback", symbol=symbols[0],
                timeframe="H1", mode="sim", start_ts=now, data_source="binance",
                poll_interval=args.poll_interval,
            )
        except Exception as e:
            _logger.warning("DB write_run_metadata failed: %s", e)

    def _db_write(fn: Callable, *a: Any, **kw: Any) -> None:
        """Guard DB writes: skip if --no-db, log warning on failure."""
        if args.no_db:
            return
        try:
            fn(*a, **kw)
        except Exception as e:
            _logger.warning("DB %s failed: %s", fn.__name__, e)

    def fetcher(symbol: str, timeframe: str, limit: int, **kwargs) -> pd.DataFrame:
        return adapter.fetch_ohlcv(symbol, timeframe, limit, **kwargs)

    def on_signal(symbol: str, action: Action, price: float, ts: datetime) -> None:
        signal_type = "entry" if action.type in ("buy", "sell") else "exit"
        strength = 1.0 if action.type == "buy" else (
            -1.0 if action.type == "sell" else 0.0
        )
        _db_write(
            write_signal,
            ts=ts, run_id=run_id, strategy="trendpullback",
            symbol=symbol, timeframe="H1", signal_type=signal_type,
            source="sim", price=price, signal_strength=strength,
        )

    strategy = TrendPullbackStrategy(max_hold_bars=args.max_hold_bars)
    executor = LiveExecutor(
        cost_model, simulation=True, telegram=telegram,
        strategy_name="TrendPullback",
    )

    def on_bar(rid: str, ts: datetime, equity: float, drawdown: float, ret_1d: float) -> None:
        _db_write(write_equity_point, ts=ts, run_id=rid, equity=equity, drawdown=drawdown, ret_1d=ret_1d)

    def on_trade(trade: dict) -> None:
        _db_write(write_trade, **trade)
        _refresh_performance()

    def on_ohlcv(rid: str, symbol: str, timeframe: str, bar: dict, ts: datetime) -> None:
        row = pd.DataFrame([{
            "ts": ts,
            "open": bar.get("open", 0),
            "high": bar.get("high", 0),
            "low": bar.get("low", 0),
            "close": bar.get("close", 0),
            "volume": bar.get("volume", 0),
        }]).set_index("ts")
        _db_write(write_ohlcv, row, symbol, timeframe, rid, source="sim")

    def on_heartbeat(rid: str) -> None:
        _db_write(update_heartbeat, rid)

    def _refresh_performance() -> None:
        if args.no_db:
            return
        try:
            from types import SimpleNamespace

            from db.timescale_reader import load_equity_curve, load_trade_blotter

            eq_df = load_equity_curve(run_id)
            if eq_df.empty or len(eq_df) < 2:
                return

            # WHY: load_equity_curve returns _time as a column, not the index
            start_ts = eq_df["_time"].iloc[0].to_pydatetime()
            end_ts = eq_df["_time"].iloc[-1].to_pydatetime()

            trades_df = load_trade_blotter(run_id)
            trade_rows = trades_df.to_dict("records") if not trades_df.empty else []

            # WHY: compute_all expects duck-typed objects with specific attributes.
            # We build minimal shims from DB data rather than importing heavy types.
            shim = SimpleNamespace(
                equity_curve=[SimpleNamespace(ts=row["_time"], equity=row["equity"])
                              for row in eq_df.to_dict("records")],
                trades=[SimpleNamespace(
                    gross_pnl=r.get("gross_pnl", 0), net_pnl=r.get("net_pnl", 0),
                    commission=r.get("commission", 0), slippage=r.get("slippage", 0),
                    tax=0, entry_ts=r.get("entry_ts"), exit_ts=r.get("exit_ts"),
                    entry_price=r.get("entry_price", 0),
                    holding_bars=r.get("holding_bars", 0),
                ) for r in trade_rows],
            )
            metrics = compute_all(shim, start_ts, end_ts)
            write_performance(run_id, metrics)
        except Exception as e:
            _logger.warning("_refresh_performance failed: %s", e)

    runner = LiveRunner(
        strategy=strategy,
        symbols=symbols,
        fetcher=fetcher,
        feature_fn=prepare_signals,
        executor=executor,
        run_id=run_id,
        timeframe="1h",
        warmup_bars=720,
        initial_balance=args.initial_balance,
        poll_interval=args.poll_interval,
        on_signal=on_signal,
        on_bar=on_bar,
        on_trade=on_trade,
        on_ohlcv=on_ohlcv,
        on_heartbeat=on_heartbeat,
    )

    print(f"Sim started: symbols={symbols}, poll={args.poll_interval}s, run_id={run_id}")
    runner.run()


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
            gross_return=float(t.gross_return), net_return=float(t.net_return),
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
    p.add_argument("--mode", default="backtest", choices=["backtest", "sim", "live"])
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--market", default="crypto")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--initial-balance", type=float, default=100_000)
    p.add_argument("--max-hold-bars", type=int, default=24)
    p.add_argument("--sample", default="oos")
    p.add_argument("--out-dir", default="data/backtests")
    p.add_argument("--no-annualize", action="store_true",
                   help="skip annualized metrics (sharpe, sortino, calmar, CAGR)")
    p.add_argument("--poll-interval", type=int, default=60,
                   help="seconds between poll cycles (sim mode)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-db", action="store_true", help="skip writing to TimescaleDB")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    dispatch = {"backtest": run_backtest, "sim": run_sim, "live": run_live}
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
