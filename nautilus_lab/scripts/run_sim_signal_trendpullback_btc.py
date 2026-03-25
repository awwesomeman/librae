#!/usr/bin/env python3
"""Sim-live signal runner for TrendPullBack on Binance BTC.

Generates simulated trading signals on a loop, optionally pushing to
Telegram and/or InfluxDB. Designed for paper/sim mode only.

Usage:
    python -m nautilus_lab.scripts.run_sim_signal_trendpullback_btc [--once] [--telegram]

Environment variables:
    TELEGRAM_BOT_TOKEN  - Telegram bot token (optional)
    TELEGRAM_CHAT_ID    - Telegram chat ID (optional)
    TELEGRAM_ENABLED    - "true" to enable (optional)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from nautilus_lab.adapters.telegram import TelegramAdapter
from nautilus_lab.strategies.trendpullback_btc import (
    Signal,
    TrendPullBackParams,
    generate_signals,
)
from nautilus_lab.scripts._shared_data import prepare_sample_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SYMBOL = "BTCUSDT"
STRATEGY = "trendpullback"
SCAN_INTERVAL_SECONDS = 60


def _format_signal(sig: Signal) -> str:
    return (
        f"[{STRATEGY}] {sig.side.upper()} {SYMBOL}\n"
        f"  Entry: {sig.entry_price:.2f}\n"
        f"  Stop:  {sig.stop_price:.2f}\n"
        f"  Target: {sig.target_price:.2f}\n"
        f"  Strength: {sig.strength:.2f}\n"
        f"  Time: {sig.ts}"
    )


def run_once(
    params: TrendPullBackParams,
    telegram: TelegramAdapter | None = None,
) -> list[Signal]:
    """Single scan: generate signals and optionally notify."""
    m1, h1, d1 = prepare_sample_data()

    signals = generate_signals(m1, h1, d1, params)
    logger.info("Generated %d signals", len(signals))

    for sig in signals[-5:]:  # only report latest 5
        msg = _format_signal(sig)
        logger.info("Signal:\n%s", msg)
        if telegram and telegram.enabled:
            telegram.send_signal(
                strategy=STRATEGY,
                symbol=SYMBOL,
                side=sig.side,
                price=sig.entry_price,
                stop=sig.stop_price,
                target=sig.target_price,
                extra={"strength": f"{sig.strength:.2f}"},
            )

    return signals


def main() -> None:
    parser = argparse.ArgumentParser(description="Sim-live signal runner")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--telegram", action="store_true", help="Enable Telegram notifications")
    parser.add_argument("--interval", type=int, default=SCAN_INTERVAL_SECONDS, help="Scan interval (seconds)")
    args = parser.parse_args()

    params = TrendPullBackParams()
    telegram = TelegramAdapter(enabled=args.telegram) if args.telegram else None

    if args.once:
        signals = run_once(params, telegram=telegram)
        print(f"Total signals: {len(signals)}")
        return

    logger.info("Starting sim-live signal loop (interval=%ds, telegram=%s)", args.interval, args.telegram)
    while True:
        try:
            run_once(params, telegram=telegram)
        except KeyboardInterrupt:
            logger.info("Shutting down")
            break
        except Exception:
            logger.exception("Error in signal loop")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
