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

from librae.core.run_config import RunConfig  # noqa: E402


def make_test_cfg(**overrides) -> RunConfig:
    """Build a minimal RunConfig for tests (no_db=True)."""
    defaults = dict(
        strategy_name="test",
        symbols=["BTCUSDT"],
        timeframe="H1",
        market="crypto",
        data_source="binance_spot",
        initial_balance=100_000.0,
        mode="sim",
        no_db=True,
        poll_seconds=0,
        params={},
    )
    defaults.update(overrides)
    return RunConfig(**defaults)
