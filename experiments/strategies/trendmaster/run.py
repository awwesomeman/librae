#!/usr/bin/env python3
"""TrendMaster Pro 2.3 strategy runner — experiment variant.

Multi-indicator filtered MA crossover with long/short support.

Usage:
    python -m experiments.trendmaster.run --mode backtest --dry-run
    python -m experiments.trendmaster.run --mode sim
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from librae import Backtest
from librae.config.market_config import get_market
from db.timescale_writer import write_backtest_output, write_ohlcv

from .strategy import TrendMasterStrategy
from .utils import fetch_and_prepare, prepare_signals

logger = logging.getLogger("experiments.trendmaster")

STRATEGY_NAME = Path(__file__).parent.name


def _build_strategy(params: dict) -> TrendMasterStrategy:
    return TrendMasterStrategy(
        sl_percent=params.get("sl_percent", 3.0),
        tp_percent=params.get("tp_percent", 15.0),
        sl_trail=params.get("sl_trail", True),
        max_hold_bars=params.get("max_hold_bars", 144),
        tp_reached_limit=params.get("tp_reached_limit", 3),
        sl_reached_limit=params.get("sl_reached_limit", 3),
    )


def run_backtest(args: argparse.Namespace) -> None:
    """Run backtest mode with M5 data."""
    scfg = args.strategy
    params = scfg["params"]
    symbol = scfg["symbol"]
    timeframe = scfg["timeframe"]

    start = params.get("start")
    end = params.get("end")
    period_str = f"{start} ~ {end}" if start else f"{params['months']} months"
    logger.info("[1/3] Fetching & preparing %s (%s, %s)...", symbol, period_str, timeframe)
    df = fetch_and_prepare(symbol, params["months"], start=start, end=end,
                           timeframe=timeframe, params=params)
    logger.info("       bars=%d", len(df))

    logger.info("[2/3] Running backtest...")
    strategy = _build_strategy(params)
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
        logger.info("[DRY-RUN] Done.")
        return

    if not args.no_db:
        try:
            counts = write_backtest_output(output)
            ohlcv_df = df.droplevel("symbol")[["open", "high", "low", "close", "volume"]]
            ohlcv_df.index.name = "ts"
            counts["ohlcv"] = write_ohlcv(ohlcv_df, symbol, timeframe, data_source="binance_spot")
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

    strategy = _build_strategy(params)

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
    )
    logger.info("Sim started: strategy=%s, symbols=%s, timeframe=%s, poll=%ds",
                STRATEGY_NAME, symbols, timeframe, args.poll_interval)
    trader.run()


def run_live(args: argparse.Namespace) -> None:
    raise NotImplementedError("Live mode not yet implemented.")


def parse_args() -> argparse.Namespace:
    from librae.cli import base_parser, parse_with_config

    p = base_parser("TrendMaster Pro 2.3 strategy")
    return parse_with_config(p, config_path=Path(__file__).parent / "config.yaml")


def main() -> None:
    from librae.cli import setup_logging

    setup_logging()
    args = parse_args()
    dispatch = {"backtest": run_backtest, "sim": run_sim, "live": run_live}
    dispatch[args.mode](args)


if __name__ == "__main__":
    main()
