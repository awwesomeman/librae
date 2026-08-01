#!/usr/bin/env python3
"""Alert-delivery rehearsal — issue #86 DoD: "Alert delivery (Telegram,
database, stale-data) is exercised end to end with an injected failure,
confirming the operator actually receives the alert."

Drives a real ``LiveTrader`` (mode=sim, no broker/DB needed) through the same
``_notify``/``_check_staleness`` engine code paths production uses, plus
``librae.orchestration.live``'s DB-write-failure wrapper, with a real
``TelegramAdapter`` so the alert is an actual message in the configured chat
— not a mocked assertion. Each scenario is independent and injects one
failure mode:

- stale-data:  fetcher returns a bar older than the staleness threshold.
- poll-error:  fetcher raises on every call for 3 consecutive cycles.
- db-write:    calls the orchestration DB-write wrapper with a failing sink,
               3 times, the same way analytics callbacks fail in production.

Usage:
    uv run python scripts/rehearse_alerts.py                  # all 3 scenarios
    uv run python scripts/rehearse_alerts.py --scenario stale-data
    uv run python scripts/rehearse_alerts.py --scenario poll-error
    uv run python scripts/rehearse_alerts.py --scenario db-write

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment (.env).
Record the result (message received? title/content correct?) in
docs/guides/operational-runbook.md's rehearsal log.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from librae.core.run_config import AccountConfig, ExecutionPolicy, RunConfig, RuntimePolicy
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.live.engine import LiveTrader
from librae.live.state import MemoryLiveStateStore
from librae.notifications.config import TelegramConfig
from librae.notifications.telegram import TelegramAdapter, TelegramCredentials
from librae.orchestration.live import _DB_FAILURE_ALERT_THRESHOLD, _TimescaleCallbacks

SYMBOL = "BTCUSDT"
TIMEFRAME = "M1"


class _HoldStrategy(Strategy):
    """Never trades — alert plumbing is the point, not order flow."""

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


def _feature_fn(h1_base: pd.DataFrame) -> pd.DataFrame:
    df = h1_base.copy()
    df["entry_signal"] = False
    df["exit_signal"] = False
    return df


def _make_ohlcv(end_ts: datetime, n: int = 5) -> pd.DataFrame:
    ts = pd.date_range(end=end_ts, periods=n, freq="1min")
    close = np.full(n, 65_000.0)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(n, 1.0),
        }
    )


def _build_config() -> RunConfig:
    return RunConfig(
        strategy_name="ops_rehearsal",
        symbols=[SYMBOL],
        timeframe=TIMEFRAME,
        market="crypto",
        data_source="binance_spot",
        account=AccountConfig(currency="USDT", initial_cash=100_000.0),
        mode="sim",
        execution=ExecutionPolicy(max_bar_volume_participation_rate=None, warmup_periods=1),
        runtime=RuntimePolicy(poll_seconds=1),
    )


def _build_notifier() -> TelegramAdapter:
    adapter = TelegramAdapter(
        config=TelegramConfig(enabled=True),
        credentials=TelegramCredentials.from_env("TELEGRAM"),
    )
    if not adapter.enabled:
        sys.exit(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — cannot rehearse alert delivery. "
            "Set them in .env first."
        )
    return adapter


def _build_trader(fetcher, notifier: TelegramAdapter) -> LiveTrader:
    return LiveTrader(
        _HoldStrategy(),
        _feature_fn,
        config=_build_config(),
        adapter=fetcher,
        notifier=notifier,
        state_store=MemoryLiveStateStore(),
    )


def rehearse_stale_data(notifier: TelegramAdapter) -> None:
    """A feed that stops updating: every fetch returns the same old bar."""
    print(f"[stale-data] expect '[ops_rehearsal] Stale Data: {SYMBOL}' in Telegram...")
    stale_end = datetime.now(UTC) - timedelta(minutes=10)
    fetcher = lambda *a, **kw: _make_ohlcv(stale_end)  # noqa: E731
    trader = _build_trader(fetcher, notifier)
    trader.run(max_iterations=1)
    print("[stale-data] done — check the chat.")


def rehearse_poll_error(notifier: TelegramAdapter) -> None:
    """A feed/broker that raises on every call — 3 consecutive failures alert."""
    print("[poll-error] expect '[ops_rehearsal] Poll Error' in Telegram after 3 cycles...")

    def fetcher(*_a, **_kw):
        raise ConnectionError("synthetic feed outage (rehearsal)")

    trader = _build_trader(fetcher, notifier)
    trader.run(max_iterations=LiveTrader.CONSECUTIVE_ERROR_THRESHOLD)
    print("[poll-error] done — check the chat.")


def rehearse_db_write(notifier: TelegramAdapter) -> None:
    """A failing analytics sink — 3 consecutive DB-write failures alert.

    The DB-write retry/alert threshold now lives in
    ``librae.orchestration.live._TimescaleCallbacks`` (the engine itself no
    longer owns analytics persistence), so this drives that wrapper directly
    instead of standing up a real broken DB connection.
    """
    print("[db-write] expect '[ops_rehearsal] DB Write Failing' in Telegram...")
    callbacks = _TimescaleCallbacks(_build_config(), instruments={}, notifier=notifier)

    def failing_sink(*_a, **_kw):
        raise ConnectionError("synthetic DB outage (rehearsal)")

    for _ in range(_DB_FAILURE_ALERT_THRESHOLD):
        callbacks._write(failing_sink)
    print("[db-write] done — check the chat.")


SCENARIOS = {
    "stale-data": rehearse_stale_data,
    "poll-error": rehearse_poll_error,
    "db-write": rehearse_db_write,
}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    args = parser.parse_args()

    notifier = _build_notifier()
    scenarios = SCENARIOS if args.scenario == "all" else {args.scenario: SCENARIOS[args.scenario]}
    for fn in scenarios.values():
        fn(notifier)
    print(
        f"\nSent {len(scenarios)} rehearsal alert(s). Confirm each arrived with the expected "
        "title/content, then record it in docs/guides/operational-runbook.md."
    )


if __name__ == "__main__":
    main()
