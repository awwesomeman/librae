#!/usr/bin/env python3
"""Periodic signal monitor scheduler — runs run_monitor every hour and writes to InfluxDB.

Usage:
    python scripts/monitor/scheduler.py                 # continuous hourly loop
    python scripts/monitor/scheduler.py --once          # run once then exit
    python scripts/monitor/scheduler.py --once --dry-run  # run once, print only, no InfluxDB write

Environment variables:
    INFLUX_URL      (default: http://localhost:8086)
    INFLUX_TOKEN    (required — warns and skips write if absent)
    INFLUX_ORG      (default: quant_research)
    INFLUX_BUCKET   (default: nautilus_signals)
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
        "influx_url": os.environ.get("INFLUX_URL", "http://localhost:8086"),
        "influx_token": os.environ.get("INFLUX_TOKEN", ""),
        "influx_org": os.environ.get("INFLUX_ORG", "quant_research"),
        "influx_bucket": os.environ.get("INFLUX_BUCKET", "nautilus_signals"),
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


def _write_to_influx(pt, cfg: dict) -> bool:
    """Write a Point to InfluxDB. Returns True on success."""
    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        client = InfluxDBClient(
            url=cfg["influx_url"],
            token=cfg["influx_token"],
            org=cfg["influx_org"],
        )
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(
            bucket=cfg["influx_bucket"],
            org=cfg["influx_org"],
            record=pt,
        )
        client.close()
        return True
    except Exception as exc:
        logger.error("InfluxDB write failed: %s", exc)
        return False


def run_job(cfg: dict | None = None, dry_run: bool = False) -> dict | None:
    """Execute one monitoring cycle. Returns a summary dict or None on failure."""
    if cfg is None:
        cfg = _env_config()

    now = datetime.now(timezone.utc)
    ts_label = now.strftime("%H:%M")

    try:
        from quant_lab.monitoring.signal_monitor import run_monitor

        adapter = _build_adapter(cfg)
        pt = run_monitor(
            adapter,
            symbol=cfg["symbol"],
            timeframe=cfg["timeframe"],
            source="scheduler",
            run_id="scheduler",
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
        logger.info("[%s] --dry-run: skipping InfluxDB write", ts_label)
        logger.info("[line_protocol] %s", line)
        return summary

    if not cfg["influx_token"]:
        logger.warning(
            "[%s] INFLUX_TOKEN not set — skipping InfluxDB write", ts_label
        )
        return summary

    try:
        ok = _write_to_influx(pt, cfg)
    except Exception as exc:
        logger.error("[%s] InfluxDB write exception: %s", ts_label, exc)
        ok = False
    if ok:
        logger.info("[%s] ✅ Written to InfluxDB (bucket=%s)", ts_label, cfg["influx_bucket"])

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
    parser.add_argument("--dry-run", action="store_true", help="Skip InfluxDB write")
    args = parser.parse_args()

    cfg = _env_config()
    logger.info(
        "Config: symbol=%s timeframe=%s influx_url=%s org=%s bucket=%s token_set=%s",
        cfg["symbol"],
        cfg["timeframe"],
        cfg["influx_url"],
        cfg["influx_org"],
        cfg["influx_bucket"],
        bool(cfg["influx_token"]),
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
