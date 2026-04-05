#!/usr/bin/env python3
"""Heartbeat monitor — alerts via Telegram when sim/live services go stale.

Queries backtest_runs for active sim/live runs whose last_heartbeat exceeds
the expected interval (default: 3× poll_seconds). Designed to run as a cron
job or standalone watchdog, independent of the monitored services.

Usage:
    # One-shot check
    python scripts/check_heartbeat.py

    # Continuous polling (every 60s)
    python scripts/check_heartbeat.py --loop --interval 60

    # Cron (every 5 minutes)
    */5 * * * * cd /path/to/quant-strategy-lab && .venv/bin/python scripts/check_heartbeat.py
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from db import get_conn
from librae.config.notification import TelegramConfig
from librae.notifications.telegram import EMOJI_WARNING, TelegramAdapter, TelegramCredentials

logger = logging.getLogger(__name__)

# WHY: 3× poll_seconds allows for transient delays (network blips, GC pauses)
# without false alarms. A single missed heartbeat is normal; 3 consecutive
# misses strongly indicates the service is down.
STALE_MULTIPLIER = 3


def find_stale_runs() -> list[dict[str, str]]:
    """Query DB for sim/live runs with stale heartbeats."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT run_id, strategy, symbol, mode, poll_seconds, last_heartbeat
            FROM backtest_runs
            WHERE mode IN ('sim', 'live')
              AND last_heartbeat IS NOT NULL
              AND last_heartbeat < NOW() - (poll_seconds * %s || ' seconds')::interval
        """, (STALE_MULTIPLIER,))
        rows = cur.fetchall()
        cur.close()

    return [
        {
            "run_id": r[0],
            "strategy": r[1],
            "symbol": r[2],
            "mode": r[3],
            "poll_seconds": r[4],
            "last_heartbeat": r[5].isoformat() if r[5] else "unknown",
        }
        for r in rows
    ]


def check_and_alert(adapter: TelegramAdapter) -> int:
    """Check for stale runs and send alerts. Returns number of alerts sent."""
    stale = find_stale_runs()
    if not stale:
        logger.debug("All services healthy")
        return 0

    for run in stale:
        logger.warning(
            "Stale heartbeat: %s/%s (last: %s)",
            run["strategy"], run["symbol"], run["last_heartbeat"],
        )
        adapter.send_alert(
            title=f"{EMOJI_WARNING} [{run['strategy']}] Heartbeat Timeout",
            message=f"Symbol: {run['symbol']}\nLast seen: {run['last_heartbeat']}\nService may be down. Check logs.",
        )
    return len(stale)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Heartbeat monitor for sim/live services")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--interval", type=int, default=60, help="seconds between checks (with --loop)")
    args = parser.parse_args()

    config = TelegramConfig(enabled=True)
    creds = TelegramCredentials.from_env("TELEGRAM")
    adapter = TelegramAdapter(config=config, credentials=creds)

    if not adapter.enabled:
        logger.error("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return

    if args.loop:
        logger.info("Heartbeat monitor started (interval=%ds, stale=%d×poll)",
                     args.interval, STALE_MULTIPLIER)
        while True:
            try:
                check_and_alert(adapter)
            except Exception:
                logger.exception("Check failed, will retry")
            time.sleep(args.interval)
    else:
        count = check_and_alert(adapter)
        logger.info("Done. %d stale run(s) found.", count)


if __name__ == "__main__":
    main()
