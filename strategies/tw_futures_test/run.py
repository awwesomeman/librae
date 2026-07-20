#!/usr/bin/env python3
"""tw_futures_test runner — THROWAWAY, not a real trading strategy.

Sole purpose: drive an end-to-end "strategy signal -> LiveTrader ->
place_order" test against Shioaji's sandbox (mode=live + SHIOAJI_SANDBOX=true,
see architecture.md's "mode vs sandbox" section). backtest mode is
intentionally unimplemented — this strategy has no real signal logic to
backtest. Delete this directory once the sandbox test is done.

Usage:
    python -m strategies.tw_futures_test.run --mode live --poll-seconds 10
"""
from __future__ import annotations

import logging
from pathlib import Path

from librae.core.run_config import RunConfig
from librae.live.engine import LiveTrader

from .strategy import AlwaysFlipStrategy

logger = logging.getLogger("strategies.tw_futures_test")

STRATEGY_NAME = Path(__file__).parent.name


def run_backtest(cfg: RunConfig) -> None:
    raise NotImplementedError(
        "tw_futures_test is sim/live only -- a throwaway wiring test, not a "
        "real strategy with signal logic to backtest."
    )


def run_realtime(cfg: RunConfig) -> None:
    """Run sim/live mode."""
    strategy = AlwaysFlipStrategy()
    trader = LiveTrader(strategy, lambda h1_base: h1_base, cfg=cfg)
    trader.run()


def main() -> None:
    from librae.cli import run_dispatch
    run_dispatch(STRATEGY_NAME, __file__, run_backtest, run_realtime)


if __name__ == "__main__":
    main()
