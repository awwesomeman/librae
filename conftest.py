"""Root conftest — auto-skip tests whose dependencies are missing."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# librae is now a top-level package; no extra sys.path needed.

# Tests that depend on packages not in the core install.
# nautilus_trader: separate sub-project (librae/), not a root dependency.
# shioaji: optional tw-live extra.
_NAUTILUS_TESTS = [
    "test_backtest_adapter.py",
    "test_baseline_btc_h1.py",
    "test_integration_smallsample_backtest.py",
    "test_integration_smoke.py",
    "test_metrics_module.py",
    "test_regression_baselines.py",
    "test_research_modules.py",
    "test_schema_builder.py",
    "test_telegram_adapter.py",
    "test_strategy_trendpullback_btc.py",
]

_TW_LIVE_TESTS = [
    "test_monitor_core.py",
]

_tests_dir = pathlib.Path(__file__).parent / "tests"

collect_ignore: list[str] = []

if importlib.util.find_spec("nautilus_trader") is None:
    collect_ignore.extend(str(_tests_dir / f) for f in _NAUTILUS_TESTS)

if importlib.util.find_spec("shioaji") is None:
    collect_ignore.extend(str(_tests_dir / f) for f in _TW_LIVE_TESTS)
