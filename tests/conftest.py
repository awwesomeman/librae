"""Shared test fixtures and helpers."""

from __future__ import annotations

from librae.core.run_config import RunConfig


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
