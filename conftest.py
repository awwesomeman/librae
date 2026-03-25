"""Root conftest — auto-skip tests whose dependencies are missing."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

# Tests that depend on packages not in the core install.
# nautilus_trader: separate sub-project (nautilus_lab/), not a root dependency.
# shioaji: optional tw-live extra.
_NAUTILUS_TESTS = [
    "test_backtest_adapter.py",
    "test_influx_actor.py",
    "test_integration_smallsample_backtest.py",
    "test_integration_smoke.py",
    "test_regression_baselines.py",
    "test_research_modules.py",
    "test_schema_builder.py",
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
