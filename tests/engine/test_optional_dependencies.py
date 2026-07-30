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
