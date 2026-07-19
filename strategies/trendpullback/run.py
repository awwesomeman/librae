#!/usr/bin/env python3
"""TrendPullback H1 strategy runner.

Usage:
    python -m strategies.trendpullback.run --mode backtest --dry-run
    python -m strategies.trendpullback.run --mode sim
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from strategies.data.ohlcv import get_ohlcv
from librae import Backtest
from librae.core.run_config import RunConfig
from librae.live.engine import LiveTrader
from db.timescale_writer import save_strategy_results

from .strategy import TrendPullbackStrategy
from .utils import prepare_signals

logger = logging.getLogger("strategies.trendpullback")

STRATEGY_NAME = Path(__file__).parent.name


def run_backtest(cfg: RunConfig) -> None:
    """Run backtest mode."""
    params = cfg.params or {}
    strategy = TrendPullbackStrategy(max_hold_periods=params.get("max_hold_periods", 24))

    logger.info("[1/3] Fetching & preparing %s %s...", cfg.symbol, cfg.timeframe)
    raw = get_ohlcv(cfg.symbol, cfg.timeframe, data_source=cfg.data_source,
                    start=cfg.start, end=cfg.end,
                    warmup_periods=params.get("warmup_periods", 0))
    h1 = raw.set_index("timestamp")
    h1.index.name = "ts"
    h1 = prepare_signals(h1, params)
    mi = pd.MultiIndex.from_arrays([[cfg.symbol] * len(h1), h1.index], names=["symbol", "datetime"])
    df = h1.set_index(mi)
    logger.info("       bars=%d", len(df))

    logger.info("[2/3] Running backtest...")
    bt = Backtest(data=df, strategy=strategy, cfg=cfg)
    benchmark_prices = df.xs(cfg.symbol, level="symbol")["close"]
    bt.add_benchmark(benchmark_prices)
    bt.run()

    output = bt.build_output()
    metrics = output.metrics
    sharpe_str = f"{metrics.sharpe:.3f}" if metrics.sharpe is not None else "N/A"
    logger.info("       trades=%d  sharpe=%s  mdd=%.4f  ret=%.4f",
                metrics.trades, sharpe_str, metrics.max_drawdown, metrics.total_return)

    if not cfg.no_db:
        try:
            counts = save_strategy_results(output, df, cfg)
            logger.info("       DB: %s", counts)
        except Exception as e:
            logger.warning("DB write skipped: %s", e)


def run_realtime(cfg: RunConfig) -> None:
    """Run sim/live mode."""
    params = cfg.params or {}
    strategy = TrendPullbackStrategy(max_hold_periods=params.get("max_hold_periods", 24))
    trader = LiveTrader(strategy, prepare_signals, cfg=cfg)
    trader.run()


def main() -> None:
    from librae.cli import run_dispatch
    run_dispatch(STRATEGY_NAME, __file__, run_backtest, run_realtime)


if __name__ == "__main__":
    main()
