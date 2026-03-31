#!/usr/bin/env python3
"""TrendPullback H1 strategy runner.

Usage:
    python -m strategies.trendpullback.run --mode backtest --dry-run
    python -m strategies.trendpullback.run --mode backtest --symbol BTCUSDT --months 3
    python -m strategies.trendpullback.run --mode sim
    python -m strategies.trendpullback.run --config strategies/trendpullback/config.yaml --mode sim
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from librae import Backtest
from librae.backtest.persistence import save_output
from librae.config.market_config import get_market
from db.timescale_writer import write_backtest_output, write_ohlcv

from .strategy import TrendPullbackStrategy
from .utils import fetch_and_prepare

logger = logging.getLogger("strategies.trendpullback")

TIMEFRAME = "H1"
WARMUP_BARS = 720


def run_backtest(args: argparse.Namespace) -> None:
    """Run backtest mode."""
    logger.info("[1/3] Fetching & preparing %s (%d months)...", args.symbol, args.months)
    df = fetch_and_prepare(args.symbol, args.months)
    logger.info("       bars=%d", len(df))

    logger.info("[2/3] Running backtest...")
    strategy = TrendPullbackStrategy(max_hold_bars=args.max_hold_bars)
    market_config = get_market(args.market)
    bt = Backtest(
        data=df,
        strategy=strategy,
        market_config=market_config,
        initial_balance=args.initial_balance,
    )
    benchmark_prices = df.xs(args.symbol, level="instrument")["close"]
    bt.add_benchmark(benchmark_prices)
    bt.run()

    output = bt.build_output(annualize=not args.no_annualize)
    metrics = output.metrics
    sharpe_str = f"{metrics.sharpe:.3f}" if metrics.sharpe is not None else "N/A"
    logger.info("       trades=%d  sharpe=%s  mdd=%.4f  ret=%.4f",
                metrics.trades, sharpe_str, metrics.max_drawdown, metrics.total_return)

    if args.dry_run:
        logger.info("[3/3] [DRY-RUN] Done.")
        return

    paths = save_output(output, Path(args.out_dir))
    logger.info("[3/3] Saved: %s", paths['json'])

    if not args.no_db:
        try:
            counts = write_backtest_output(output)
            ohlcv_df = df.droplevel("instrument")[["open", "high", "low", "close", "volume"]]
            ohlcv_df.index.name = "ts"
            counts["ohlcv"] = write_ohlcv(ohlcv_df, args.symbol, TIMEFRAME, bt.run_id)
            logger.info("       DB: %s", counts)
        except Exception as e:
            logger.warning("DB write skipped: %s", e)


def run_sim(args: argparse.Namespace) -> None:
    """Run sim mode — delegates infrastructure to sim_wiring."""
    from librae.live.wiring import build_live_trader
    from .utils import prepare_signals

    strategy = TrendPullbackStrategy(max_hold_bars=args.max_hold_bars)
    symbols = [s.strip() for s in args.symbol.split(",")]

    trader = build_live_trader(
        strategy=strategy,
        strategy_name="trendpullback",
        feature_fn=prepare_signals,
        symbols=symbols,
        timeframe=TIMEFRAME,
        market=args.market,
        initial_balance=args.initial_balance,
        poll_interval=args.poll_interval,
        warmup_bars=WARMUP_BARS,
        no_db=args.no_db,
    )
    logger.info("Sim started: strategy=trendpullback, symbols=%s, poll=%ds", symbols, args.poll_interval)
    trader.run()


def run_live(args: argparse.Namespace) -> None:
    raise NotImplementedError("Live mode not yet implemented.")


def parse_args() -> argparse.Namespace:
    from librae.cli import base_parser, parse_with_config

    p = base_parser("TrendPullback H1 strategy")
    p.add_argument("--max-hold-bars", type=int, default=24)
    p.add_argument("--sample", default="oos")
    return parse_with_config(p)


def main() -> None:
    from librae.cli import setup_logging

    setup_logging()
    args = parse_args()
    dispatch = {"backtest": run_backtest, "sim": run_sim, "live": run_live}
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
