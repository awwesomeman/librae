#!/usr/bin/env python3
"""TrendPullback M5 strategy runner — low-timeframe variant for signal testing.

M30 trend gate + M5 entry/exit. Same logic as trendpullback, faster signals.

Usage:
    python -m strategies.trendpullback_m5.run --mode sim
    python -m strategies.trendpullback_m5.run --mode backtest --dry-run
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from librae import Backtest
from librae.backtest.persistence import save_output
from librae.config.market_config import get_market
from db.timescale_writer import persist_backtest

from .strategy import TrendPullbackM5Strategy
from .utils import fetch_and_prepare, prepare_signals

logger = logging.getLogger("strategies.trendpullback_m5")

STRATEGY_NAME = Path(__file__).parent.name


def run_backtest(args: argparse.Namespace) -> None:
    """Run backtest mode with M5 data."""
    scfg = args.strategy
    params = scfg["params"]
    symbol = scfg["symbol"]
    timeframe = scfg["timeframe"]

    logger.info("[1/3] Fetching & preparing %s (%d months, %s)...", symbol, params["months"], timeframe)
    df = fetch_and_prepare(symbol, params["months"])
    logger.info("       bars=%d", len(df))

    logger.info("[2/3] Running backtest...")
    strategy = TrendPullbackM5Strategy(max_hold_bars=params["max_hold_bars"])
    market_config = get_market(scfg["market"])
    bt = Backtest(
        data=df,
        strategy=strategy,
        market_config=market_config,
        initial_balance=scfg["initial_balance"],
        strategy_name=STRATEGY_NAME,
    )
    benchmark_prices = df.xs(symbol, level="symbol")["close"]
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

    paths = save_output(output)
    logger.info("[3/3] Saved: %s", paths['json'])

    if not args.no_db:
        try:
            counts = persist_backtest(output, df, symbol, timeframe, params)
            logger.info("       DB: %s", counts)
        except Exception as e:
            logger.warning("DB write skipped: %s", e)


def run_sim(args: argparse.Namespace) -> None:
    """Run sim mode — delegates infrastructure to sim_wiring."""
    from librae.live.wiring import build_live_trader

    scfg = args.strategy
    params = scfg["params"]
    symbols = [s.strip() for s in scfg["symbol"].split(",")]
    timeframe = scfg["timeframe"]

    strategy = TrendPullbackM5Strategy(max_hold_bars=params["max_hold_bars"])

    trader = build_live_trader(
        strategy=strategy,
        strategy_name=STRATEGY_NAME,
        feature_fn=prepare_signals,
        symbols=symbols,
        timeframe=timeframe,
        market=scfg["market"],
        initial_balance=scfg["initial_balance"],
        poll_interval=args.poll_interval,
        warmup_bars=params["warmup_bars"],
        no_db=args.no_db,
        telegram_config=getattr(args, "telegram", None),
        signal_column="entry_signal",
        params=params,
    )
    logger.info("Sim started: strategy=%s, symbols=%s, timeframe=%s, poll=%ds",
                STRATEGY_NAME, symbols, timeframe, args.poll_interval)
    trader.run()


def run_live(args: argparse.Namespace) -> None:
    raise NotImplementedError("Live mode not yet implemented.")


def parse_args() -> argparse.Namespace:
    from librae.cli import base_parser, parse_with_config

    p = base_parser("TrendPullback M5 strategy")
    return parse_with_config(p, config_path=Path(__file__).parent / "config.yaml")


def main() -> None:
    from librae.cli import setup_logging

    setup_logging()
    args = parse_args()
    dispatch = {"backtest": run_backtest, "sim": run_sim, "live": run_live}
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
