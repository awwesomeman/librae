"""Minimal-install dependency boundary tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_core_import_and_metrics_do_not_load_optional_packages() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = """
import importlib.abc
import sys
from datetime import UTC, datetime, timedelta

blocked = {
    "ccxt",
    "exchange_calendars",
    "httpx",
    "ib_async",
    "lightweight_charts",
    "matplotlib",
    "psycopg2",
    "shioaji",
    "yaml",
}

class OptionalPackageBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise ModuleNotFoundError(f"blocked optional package: {fullname}")
        return None

sys.meta_path.insert(0, OptionalPackageBlocker())

import librae

start = datetime(2025, 1, 1, tzinfo=UTC)
metrics = librae.compute_all(
    equity_values=[100.0, 101.0, 100.5],
    timestamps=[start + timedelta(days=offset) for offset in range(3)],
    trade_pnls=[],
    total_periods=3,
)
assert metrics.period_sharpe is not None

from librae.brokers import (
    BinanceStocksAdapter,
    CryptoAdapter,
    IBKRAdapter,
    ShioajiAdapter,
)

assert BinanceStocksAdapter
assert CryptoAdapter
assert IBKRAdapter
assert ShioajiAdapter
assert blocked.isdisjoint(sys.modules)
"""

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )


def test_a_broker_neutral_decision_does_not_load_the_adapter_package() -> None:
    """Preflight reaches librae.brokers only once a symbol resolves to a venue.

    Every run that can emit order intents supplies ``broker_for``, so gating
    the import on the callable alone made a backtest with no configured broker
    import all four reference adapters. Asserted in a subprocess because any
    other test in this session may already have imported them.
    """
    project_root = Path(__file__).resolve().parents[2]
    script = """
import sys

from librae.core.executor import validate_strategy_decision
from librae.core.strategy import OrderIntent

bars = {"BTCUSDT": {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}}
decision = [OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0, time_in_force="gtc")]

validate_strategy_decision(
    decision,
    {"BTCUSDT"},
    primary_symbol="BTCUSDT",
    bars=bars,
    positions={},
    broker_for=lambda symbol: None,
)

loaded = sorted(m for m in sys.modules if m.startswith("librae.brokers"))
assert not loaded, loaded

# The check still applies once a symbol does resolve to a venue.
try:
    validate_strategy_decision(
        decision,
        {"BTCUSDT"},
        primary_symbol="BTCUSDT",
        bars=bars,
        positions={},
        broker_for=lambda symbol: "binance",
    )
except ValueError as exc:
    assert "does not support time_in_force='gtc'" in str(exc), exc
else:
    raise AssertionError("a configured venue must still reject an unsupported lifetime")
"""

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
