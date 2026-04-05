#!/usr/bin/env python3
"""KDJ Oversold — backtest + sim runner.

Usage:
    python -m experiments.utils.kdj_oversold.run          # backtest
    python -m experiments.utils.kdj_oversold.run --sim    # sim (real-time monitoring)
"""
from __future__ import annotations

import logging

from data.ohlcv import get_ohlcv
from db.timescale_writer import save_signal_results
from librae.core.utils import generate_run_id, to_canonical

from .utils import DEFAULT_PARAMS, SIGNAL_NAME, prepare_signals

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — edit these directly, no argparse needed for experiments
# ---------------------------------------------------------------------------
SYMBOL = "BTCUSDT"
SOURCE = "binance_spot"
TIMEFRAME = "1h"
START = "2025-10-01"
POLL_SECONDS = 60
WARMUP_PERIODS = 200       # bars (sim only)


def run_backtest() -> None:
    """Fetch historical data → compute signals → save to DB."""
    logger.info("[1/3] Fetching %s %s from %s...", SYMBOL, TIMEFRAME, START)
    raw = get_ohlcv(SYMBOL, TIMEFRAME, data_source=SOURCE, start=START)
    logger.info("       bars=%d", len(raw))

    logger.info("[2/3] Computing KDJ signal (J < %d)...", DEFAULT_PARAMS["j_threshold"])
    df = raw.set_index("timestamp")
    df.index.name = "ts"
    df = prepare_signals(df)
    signal_count = int((df["entry_signal"] > 0).sum())
    logger.info("       signals=%d (%.1f%%)", signal_count, signal_count / len(df) * 100)

    logger.info("[3/3] Saving to DB...")
    timeframe = to_canonical(TIMEFRAME)
    run_id = generate_run_id(SIGNAL_NAME, SYMBOL, timeframe)
    counts = save_signal_results(df, SYMBOL, timeframe, SIGNAL_NAME, SOURCE, run_id=run_id)
    logger.info("       run_id=%s", run_id)
    logger.info("       DB: %s", counts)


def run_sim() -> None:
    """Monitor signal in real-time via LiveTrader poll loop."""
    from librae.live.wiring import build_live_trader
    from .strategy import HoldStrategy

    trader = build_live_trader(
        strategy=HoldStrategy(),
        strategy_name=SIGNAL_NAME,
        feature_fn=prepare_signals,
        symbols=[SYMBOL],
        timeframe=TIMEFRAME,
        market="crypto",
        initial_balance=0,
        poll_seconds=POLL_SECONDS,
        warmup_periods=WARMUP_PERIODS,
        no_db=False,
        signal_column="entry_signal",
        params=DEFAULT_PARAMS,
    )
    logger.info("Sim started: signal=%s, symbol=%s, timeframe=%s, poll=%ds",
                SIGNAL_NAME, SYMBOL, TIMEFRAME, POLL_SECONDS)
    trader.run()


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if "--sim" in sys.argv:
        run_sim()
    else:
        run_backtest()
