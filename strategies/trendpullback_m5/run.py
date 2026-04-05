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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data.ohlcv import get_ohlcv
from librae import Backtest
from librae.config.market_config import get_market
from librae.core.utils import interval_to_timedelta
from db.timescale_writer import save_strategy_results

from .strategy import TrendPullbackM5Strategy
from .utils import prepare_signals

logger = logging.getLogger("strategies.trendpullback_m5")

STRATEGY_NAME = Path(__file__).parent.name


def run_backtest(args: argparse.Namespace) -> None:
    """Run backtest mode with M5 data."""
    scfg = args.strategy
    params = scfg["params"]
    symbol = scfg["symbol"]
    timeframe = scfg["timeframe"]

    logger.info("[1/3] Fetching & preparing %s (%d periods, %s)...", symbol, params["periods"], timeframe)
    fetch_start = datetime.now(timezone.utc) - interval_to_timedelta(timeframe) * params["periods"]
    raw = get_ohlcv(symbol, timeframe, data_source="binance_spot", start=fetch_start.isoformat())
    m5 = raw.set_index("timestamp")
    m5.index.name = "ts"
    m5 = prepare_signals(m5, params)
    mi = pd.MultiIndex.from_arrays([[symbol] * len(m5), m5.index], names=["symbol", "datetime"])
    df = m5.set_index(mi)
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
        data_source="binance_spot",
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
        logger.info("[DRY-RUN] Done.")
        return

    if not args.no_db:
        try:
            counts = save_strategy_results(output, df, symbol, timeframe, "binance_spot", params)
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
        poll_seconds=args.poll_seconds,
        warmup_bars=params["warmup_bars"],
        no_db=args.no_db,
        telegram_config=getattr(args, "telegram", None),
        signal_column="entry_signal",
        params=params,
    )
    logger.info("Sim started: strategy=%s, symbols=%s, timeframe=%s, poll=%ds",
                STRATEGY_NAME, symbols, timeframe, args.poll_seconds)
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
