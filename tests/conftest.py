"""Shared test fixtures and helpers."""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv_for_tests() -> None:
    """Load .env from the repo root for local `pytest` runs, without overwriting existing env vars.

    librae itself never does this (see db/__init__.py) — loading .env is an
    application's responsibility, and the test suite is the application here.
    CI sets env vars directly and has no .env file, so this is a no-op there.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = (part.strip() for part in line.split("=", 1))
        if not val.startswith(("'", '"')):
            val = val.split(" #", 1)[0].rstrip()  # strip trailing ` # comment`
        val = val.strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv_for_tests()

from librae.core.run_config import (  # noqa: E402
    AccountConfig,
    ExecutionPolicy,
    ReportingPolicy,
    RunConfig,
    RuntimePolicy,
)


def make_test_cfg(**overrides) -> RunConfig:
    """Build a minimal RunConfig for tests."""
    initial_balance = overrides.pop("initial_balance", None)
    reporting = overrides.pop("reporting", None)
    reporting_values = {
        field: overrides.pop(field)
        for field in ("annualize", "risk_free_rate", "periods_per_year")
        if field in overrides
    }
    runtime = overrides.pop("runtime", None)
    runtime_values = {
        field: overrides.pop(field)
        for field in (
            "poll_seconds",
            "reconciliation_interval_seconds",
            "market_data_workers",
        )
        if field in overrides
    }
    defaults = dict(
        strategy_name="test",
        symbols=["BTCUSDT"],
        timeframe="H1",
        market="crypto",
        data_source="binance_spot",
        accounts={"default": AccountConfig(currency="USDT", initial_cash=100_000.0)},
        mode="sim",
        reporting=reporting or ReportingPolicy(**reporting_values),
        runtime=runtime or RuntimePolicy(**{"poll_seconds": 0, **runtime_values}),
        params={},
        # Most unit tests isolate sizing/risk behavior from liquidity. Tests
        # for volume participation opt in with an explicit policy.
        execution=ExecutionPolicy(max_bar_volume_participation_rate=None),
    )
    if reporting is not None and reporting_values:
        raise ValueError("pass reporting or reporting fields, not both")
    if runtime is not None and runtime_values:
        raise ValueError("pass runtime or runtime fields, not both")
    if initial_balance is not None:
        defaults["accounts"] = {
            "default": AccountConfig(currency="USDT", initial_cash=initial_balance)
        }
    defaults.update(overrides)
    return RunConfig(**defaults)
