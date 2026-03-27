"""Root conftest — auto-skip tests whose dependencies are missing."""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

# librae is now a top-level package; no extra sys.path needed.

_tests_dir = pathlib.Path(__file__).parent / "tests"

# Tests that depend on optional packages (skip if not installed).
# nautilus_trader: legacy sub-project, not a root dependency.
_NAUTILUS_TESTS = [
    "engine/test_backtest_adapter.py",
    "integration/test_baseline_btc_h1.py",
    "integration/test_integration_smoke.py",
    "engine/test_metrics_module.py",
    "integration/test_regression_baselines.py",
    "integration/test_research_modules.py",
    "app/test_schema_builder.py",
    "monitoring/test_telegram_adapter.py",
]

collect_ignore: list[str] = []

if importlib.util.find_spec("nautilus_trader") is None:
    collect_ignore.extend(str(_tests_dir / f) for f in _NAUTILUS_TESTS)
