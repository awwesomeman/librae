"""Shared factories for live-engine tests.

Test modules import these instead of instantiating one another's test classes.
"""

from __future__ import annotations

import tempfile
import weakref
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np
import pandas as pd
from librae.core.cost_model import CostModel
from librae.core.run_config import AccountConfig, ExecutionPolicy, RunConfig
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.live.engine import LiveTrader
from librae.live.execution_identity import ExecutionIdentity
from librae.live.executor import LiveExecutor
from librae.live.state import MemoryLiveStateStore

from tests.conftest import make_test_cfg

_HALT_DIRECTORY = tempfile.TemporaryDirectory(prefix="librae-halt-")
_HALT_FILES: weakref.WeakKeyDictionary[object, Path] = weakref.WeakKeyDictionary()


def halt_file_of(state_store: object) -> Path:
    """The halt file that goes with ``state_store``, as a deployment's volume goes with its store.

    A successor built on the same store finds a halt its predecessor wrote.
    """
    path = _HALT_FILES.get(state_store)
    if path is None:
        path = _HALT_FILES[state_store] = Path(_HALT_DIRECTORY.name) / uuid4().hex
    return path


def _zero_cost_model() -> CostModel:
    return CostModel.zero()


def _test_execution_identity() -> ExecutionIdentity:
    return ExecutionIdentity("fixture", "paper", "fixture", "a" * 24)


def _make_ohlcv_df(n: int = 5, start_hour: int = 0) -> pd.DataFrame:
    """Create a simple OHLCV DataFrame with known timestamps."""
    base = datetime(2025, 1, 1, start_hour, 0, 0, tzinfo=UTC)
    ts = pd.date_range(base, periods=n, freq="h", tz=UTC)
    prices = np.arange(100.0, 100.0 + n, 1.0)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": prices - 0.5,
            "high": prices + 1.0,
            "low": prices - 1.0,
            "close": prices,
            "volume": np.full(n, 1000.0),
        }
    )


TEST_CLOCK_NOW = datetime(2025, 1, 1, 10, tzinfo=UTC)


def _simple_feature_fn(h1_base: pd.DataFrame) -> pd.DataFrame:
    """Add entry_signal and exit_signal columns (all False by default)."""
    h1 = h1_base.copy()
    h1["entry_signal"] = False
    h1["exit_signal"] = False
    return h1


class _HoldStrategy(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


def _test_cfg(**overrides) -> RunConfig:
    warmup_periods = overrides.pop("warmup_periods", 5)
    overrides.setdefault(
        "execution",
        ExecutionPolicy(
            max_bar_volume_participation_rate=None,
            warmup_periods=warmup_periods,
        ),
    )
    from librae.config.symbols import load_symbol_registry

    registry = load_symbol_registry()
    symbols = overrides.get("symbols", ["BTCUSDT"])
    routes = {
        symbol: dict(values)
        for symbol, values in (overrides.get("instrument_overrides") or {}).items()
    }
    for symbol in symbols:
        if symbol not in registry:
            route = routes.setdefault(symbol, {})
            route.setdefault("instrument_type", "spot")
            route.setdefault("currency", "USDT")
            if route.get("data_adapter") != "ibkr":
                route.setdefault("calendar_id", "24/7")
    if "account" not in overrides:
        currencies = {
            routes.get(symbol, {}).get("currency") or registry[symbol].currency
            for symbol in symbols
        }
        if len(currencies) != 1:
            raise ValueError("test config symbols must share one account currency")
        overrides["account"] = AccountConfig(
            currency=next(iter(currencies)),
            initial_cash=100_000.0,
        )
    if routes:
        overrides["instrument_overrides"] = routes
    return make_test_cfg(**overrides)


def make_live_runner(
    strategy: Strategy | None = None,
    fetcher=None,
    feature_fn=None,
    executor: LiveExecutor | None = None,
    config: RunConfig | None = None,
    **kwargs,
) -> LiveTrader:
    test_config = config or _test_cfg()
    # Only the live path requires a store that declares restart durability,
    # so the flag reads as a live marker rather than boilerplate.
    kwargs.setdefault(
        "state_store",
        MemoryLiveStateStore(restart_durable_for_tests=test_config.mode == "live"),
    )
    kwargs.setdefault("clock", lambda: TEST_CLOCK_NOW)
    kwargs.setdefault("on_ohlcv", None)
    if test_config.mode == "live":
        kwargs.setdefault("runtime_revision", "test-runtime")
        if kwargs["state_store"] is not None:
            kwargs.setdefault("halt_file", halt_file_of(kwargs["state_store"]))
        configured_order_adapter = kwargs.get("order_adapter")
        routes = (
            configured_order_adapter.values()
            if isinstance(configured_order_adapter, dict)
            else (configured_order_adapter,)
        )
        for route in routes:
            if route is not None:
                route.execution_identity = _test_execution_identity
    resolved_feature_fn = (
        feature_fn
        if feature_fn is not None
        else None
        if kwargs.get("batch_feature_fn") is not None
        else _simple_feature_fn
    )
    runner = LiveTrader(
        strategy or _HoldStrategy(),
        resolved_feature_fn,
        config=test_config,
        adapter=fetcher or (lambda *a, **kw: _make_ohlcv_df()),
        cost_model=(
            executor.get_cost_model(test_config.symbol) if executor else _zero_cost_model()
        ),
        on_bar=None,
        on_position_event=None,
        on_heartbeat=None,
        on_signal_outcome=None,
        warmup_fetcher=None,
        **kwargs,
    )
    runner._sleep = lambda _seconds: None  # no real delays in unit tests
    # Most fixtures use a fixed clock to exercise execution rather than
    # staleness. Dedicated staleness tests restore the production bound.
    runner.STALE_DATA_TOLERANCE_BARS = 100
    return runner


def with_run_leases(trader: LiveTrader) -> LiveTrader:
    """Mark the leases ``run()`` takes as held, for a test that drives the trader directly.

    A live trader refuses operator calls without them. The store's leases stay
    free, so a later ``run()`` or a restart on the same store still takes them.
    """
    trader._account_lease_acquired = True
    trader._lease_acquired = True
    return trader


def make_live_trader(
    strategy: Strategy,
    adapter: MagicMock,
    *,
    state_store: MemoryLiveStateStore | None = None,
    config: RunConfig | None = None,
    runtime_revision: str = "test-runtime",
    clock=None,
    on_ready=None,
    on_runtime_event=None,
    on_position_event=None,
    on_performance=None,
    cost_model: CostModel | None = None,
    fetcher=None,
) -> LiveTrader:
    state_store = state_store or MemoryLiveStateStore(restart_durable_for_tests=True)
    config = config or _test_cfg(mode="live")
    trader = LiveTrader(
        strategy,
        _simple_feature_fn,
        config=config,
        adapter=fetcher or (lambda *a, **kw: _make_ohlcv_df()),
        cost_model=cost_model or _zero_cost_model(),
        order_adapter=adapter,
        on_bar=None,
        on_position_event=on_position_event,
        on_ohlcv=None,
        on_heartbeat=None,
        on_signal_outcome=None,
        state_store=state_store,
        runtime_revision=runtime_revision,
        halt_file=halt_file_of(state_store) if config.mode == "live" else None,
        clock=clock or (lambda: TEST_CLOCK_NOW),
        on_ready=on_ready,
        on_runtime_event=on_runtime_event,
        on_performance=on_performance,
    )
    trader.STALE_DATA_TOLERANCE_BARS = 100
    return trader
