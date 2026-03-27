#!/usr/bin/env python3
"""Periodic signal monitor scheduler — runs run_monitor every hour and writes to TimescaleDB.

Usage:
    python scripts/monitor/scheduler.py                 # continuous hourly loop
    python scripts/monitor/scheduler.py --once          # run once then exit
    python scripts/monitor/scheduler.py --once --dry-run  # run once, print only, no DB write

Environment variables:
    CCXT_API_KEY    (optional — read-only mode when absent)
    CCXT_API_SECRET (optional)
    MONITOR_SYMBOL  (default: BTC/USDT)
    MONITOR_TIMEFRAME (default: 1h)

Skills: python, quant
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root on path
_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")

# ---------------------------------------------------------------------------
# Globals for graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s — shutting down gracefully", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------


def _env_config() -> dict:
    return {
        "symbol": os.environ.get("MONITOR_SYMBOL", "BTC/USDT"),
        "timeframe": os.environ.get("MONITOR_TIMEFRAME", "1h"),
        "api_key": os.environ.get("CCXT_API_KEY", ""),
        "api_secret": os.environ.get("CCXT_API_SECRET", ""),
    }


# ---------------------------------------------------------------------------
# Core job
# ---------------------------------------------------------------------------


def _build_adapter(cfg: dict):
    from quant_lab.adapters.crypto_adapter import CryptoAdapter

    return CryptoAdapter(
        exchange_id="binance",
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
    )


def _write_to_timescale(pt, cfg: dict, run_id: str) -> bool:
    """Write signal data to TimescaleDB with mode='sim'. Returns True on success."""
    try:
        # Parse InfluxDB point for field values
        line = pt.to_line_protocol()
        fields_str = line.split(" ")[1] if " " in line else ""
        field_map = {}
        for pair in fields_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                field_map[k] = v

        # Parse tags
        tag_part = line.split(" ")[0] if " " in line else ""
        tag_map = {}
        for pair in tag_part.split(",")[1:]:  # skip measurement name
            if "=" in pair:
                k, v = pair.split("=", 1)
                tag_map[k] = v

        from quant_lab.db.timescale_writer import get_conn, TIMESCALE_DSN

        now = datetime.now(timezone.utc)
        strategy = tag_map.get("strategy", "TrendPullback")
        symbol = cfg["symbol"]
        timeframe = cfg["timeframe"]

        with get_conn(TIMESCALE_DSN) as conn:
            cur = conn.cursor()

            # Ensure backtest_runs entry exists for this sim run
            cur.execute(
                """INSERT INTO backtest_runs
                   (run_id, strategy, symbol, timeframe, run_ts, mode)
                   VALUES (%s, %s, %s, %s, %s, 'sim')
                   ON CONFLICT (run_id) DO UPDATE SET run_ts = EXCLUDED.run_ts""",
                (run_id, strategy, symbol, timeframe, now),
            )

            # Write signal to strategy_signals
            price = float(field_map.get("price", "0").rstrip("i"))
            signal_strength = float(field_map.get("signal_strength", "0").rstrip("i"))
            confidence = float(field_map.get("confidence", "0").rstrip("i"))
            signal_type = tag_map.get("signal_type", "hold")

            cur.execute(
                """INSERT INTO strategy_signals
                   (ts, run_id, strategy, symbol, timeframe,
                    signal_type, source, price, signal_strength, confidence, quantity)
                   VALUES (%s, %s, %s, %s, %s, %s, 'sim', %s, %s, %s, 0)""",
                (now, run_id, strategy, symbol, timeframe,
                 signal_type, price, signal_strength, confidence),
            )
            cur.close()

        logger.info("✅ Written to TimescaleDB (run_id=%s, mode=sim)", run_id)
        return True
    except Exception as exc:
        logger.error("TimescaleDB write failed: %s", exc)
        return False


def run_job(cfg: dict | None = None, dry_run: bool = False) -> dict | None:
    """Execute one monitoring cycle. Returns a summary dict or None on failure."""
    if cfg is None:
        cfg = _env_config()

    now = datetime.now(timezone.utc)
    ts_label = now.strftime("%H:%M")

    # Fixed run_id for scheduler: scheduler-<symbol>-<strategy>
    symbol_clean = cfg["symbol"].replace("/", "").lower()
    sim_run_id = f"scheduler-{symbol_clean}-trendpullback"

    try:
        from quant_lab.monitoring.signal_monitor import run_monitor

        adapter = _build_adapter(cfg)
        pt = run_monitor(
            adapter,
            symbol=cfg["symbol"],
            timeframe=cfg["timeframe"],
            source="scheduler",
            run_id=sim_run_id,
        )
    except Exception as exc:
        logger.error("[%s] run_monitor failed: %s", ts_label, exc)
        return None

    if pt is None:
        logger.warning("[%s] run_monitor returned None (no data?)", ts_label)
        return None

    # Parse the point for logging
    line = pt.to_line_protocol()
    fields_str = line.split(" ")[1] if " " in line else ""
    field_map = {}
    for pair in fields_str.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            field_map[k] = v

    ts_ns = int(line.split(" ")[-1]) if len(line.split(" ")) >= 3 else 0
    ts_dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc) if ts_ns else now

    sig = field_map.get("signal_strength", "?")
    conf = field_map.get("confidence", "?")
    price = field_map.get("price", "?")

    summary = {
        "signal": sig,
        "confidence": conf,
        "price": price,
        "ts": ts_dt.isoformat(),
    }

    logger.info(
        "[%s] signal=%s confidence=%s price=%s ts=%s",
        ts_label, sig, conf, price, ts_dt.isoformat(),
    )

    if dry_run:
        logger.info("[%s] --dry-run: skipping TimescaleDB write", ts_label)
        logger.info("[line_protocol] %s", line)
        return summary

    # Write to TimescaleDB
    try:
        _write_to_timescale(pt, cfg, sim_run_id)
    except Exception as exc:
        logger.error("[%s] TimescaleDB write exception: %s", ts_label, exc)

    return summary


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def _run_loop_apscheduler(cfg: dict, dry_run: bool):
    """Use APScheduler for cron-style hourly execution."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler()
    sched.add_job(
        run_job,
        "cron",
        minute=0,
        kwargs={"cfg": cfg, "dry_run": dry_run},
        id="signal_monitor",
        max_instances=1,
    )
    logger.info("APScheduler started — job runs every hour at :00")
    # Run once immediately on start
    run_job(cfg=cfg, dry_run=dry_run)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


def _run_loop_sleep(cfg: dict, dry_run: bool):
    """Fallback: simple sleep loop every 3600s."""
    import time

    logger.info("Using time.sleep loop (APScheduler not available)")
    while not _shutdown:
        run_job(cfg=cfg, dry_run=dry_run)
        # Sleep in small increments for responsive shutdown
        for _ in range(360):
            if _shutdown:
                break
            time.sleep(10)
    logger.info("Sleep-loop scheduler stopped")


def main():
    parser = argparse.ArgumentParser(description="Signal monitor scheduler")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Skip TimescaleDB write")
    args = parser.parse_args()

    cfg = _env_config()
    logger.info(
        "Config: symbol=%s timeframe=%s",
        cfg["symbol"],
        cfg["timeframe"],
    )

    if args.once:
        result = run_job(cfg=cfg, dry_run=args.dry_run)
        if result:
            print(
                f"signal={result['signal']} confidence={result['confidence']} "
                f"price={result['price']} ts={result['ts']}"
            )
        else:
            print("No result returned")
        return

    # Continuous mode
    try:
        _run_loop_apscheduler(cfg, args.dry_run)
    except ImportError:
        _run_loop_sleep(cfg, args.dry_run)


if __name__ == "__main__":
    main()
