"""Tests for repository-level sim/live integration wiring."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from librae.config.symbols import resolve_symbol
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import PortfolioWeights
from librae.live.state import LiveRuntimeState, MemoryLiveStateStore, TrackedOrder
from librae.orchestration.live import (
    _build_adapter,
    _ready_callback_from_env,
    _status_interval_periods,
    _TimescaleCallbacks,
    build_live_trader,
)

from tests.conftest import make_test_cfg


def test_disabled_notifier_does_not_load_optional_integration() -> None:
    with patch.dict("sys.modules", {"librae.notifications.telegram": None}):
        from librae.orchestration.live import _build_notifier

        assert _build_notifier(None) is None
        assert _build_notifier({"enabled": False}) is None


def test_status_schedule_is_separate_from_notifier_transport() -> None:
    assert _status_interval_periods(None) is None
    assert (
        _status_interval_periods(
            {
                "enabled": True,
                "notifications": {
                    "status": {"enabled": True, "interval_periods": 6},
                },
            }
        )
        == 6
    )


def test_ready_callback_publishes_run_id_to_supervisor_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ready_file = tmp_path / "ready"
    monkeypatch.setenv("LIBRAE_READY_FILE", str(ready_file))

    callback = _ready_callback_from_env()

    assert callback is not None
    callback("run-123")
    marker = ready_file.read_text(encoding="utf-8").strip()
    run_id, separator, generation = marker.partition(":")
    assert run_id == "run-123"
    assert separator == ":"
    assert len(generation) == 32


def test_ready_callback_binds_marker_to_deployment_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ready_file = tmp_path / "ready"
    monkeypatch.setenv("LIBRAE_READY_FILE", str(ready_file))
    monkeypatch.setenv("LIBRAE_READY_TOKEN", "attempt-123")

    callback = _ready_callback_from_env()

    assert callback is not None
    callback("run-123")
    token, run_id, generation = ready_file.read_text(encoding="utf-8").strip().split(":")
    assert token == "attempt-123"
    assert run_id == "run-123"
    assert len(generation) == 32


def test_missing_db_dependency_is_reported_by_deployment_factory() -> None:
    config = make_test_cfg(mode="sim")

    with (
        patch("librae.orchestration.live._build_adapter", return_value=MagicMock()),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch.dict("sys.modules", {"librae.db.timescale_state": None}),
        pytest.raises(ModuleNotFoundError, match="Install Librae's 'db' extra"),
    ):
        build_live_trader(MagicMock(), lambda frame: frame, config=config)


def test_factory_registers_timescale_callbacks() -> None:
    config = make_test_cfg(mode="sim")
    callbacks = MagicMock()

    with (
        patch("librae.orchestration.live._build_adapter", return_value=MagicMock()),
        patch(
            "librae.orchestration.live._build_state_store",
            return_value=MemoryLiveStateStore(),
        ),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch("librae.orchestration.live._TimescaleCallbacks", return_value=callbacks),
    ):
        trader = build_live_trader(MagicMock(), lambda frame: frame, config=config)

    callbacks.register_run.assert_called_once_with(trader.run_id)


def test_database_enabled_sim_checkpoints_portfolio_weights_decision() -> None:
    class JsonRoundTripStateStore:
        def __init__(self) -> None:
            self.raw: dict[str, object] | None = None

        def load(self, state_key: str) -> LiveRuntimeState | None:
            if self.raw is None:
                return None
            return LiveRuntimeState.from_dict(self.raw)

        def save(
            self,
            state: LiveRuntimeState,
            orders: Sequence[TrackedOrder] = (),
        ) -> None:
            self.raw = json.loads(json.dumps(state.to_dict()))

        def acquire_lease(self, state_key: str) -> bool:
            return True

        def release_lease(self, state_key: str) -> None:
            pass

    frame = pd.DataFrame(
        {
            "ts": pd.date_range("2025-01-01", periods=5, freq="h", tz=UTC),
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.0] * 5,
            "volume": [1_000.0] * 5,
        }
    )
    adapter = MagicMock()
    adapter.fetch_ohlcv.return_value = frame
    strategy = MagicMock()
    strategy.on_bar.return_value = PortfolioWeights(
        weights={"BTCUSDT": 0.5},
        reason="allocate",
    )
    store = JsonRoundTripStateStore()

    with (
        patch("librae.orchestration.live._build_state_store", return_value=store),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch("librae.orchestration.live._TimescaleCallbacks", return_value=MagicMock()),
    ):
        trader = build_live_trader(
            strategy,
            lambda history: history,
            config=make_test_cfg(
                mode="sim",
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                ),
            ),
            data_adapter_overrides={"BTCUSDT": adapter},
        )

    trader._poll_cycle()

    assert store.raw is not None
    restored = LiveRuntimeState.from_dict(store.raw)
    assert restored.pending_decision == PortfolioWeights(
        weights={"BTCUSDT": 0.5},
        reason="allocate",
    )


def test_run_is_registered_before_first_checkpoint_write() -> None:
    """A durable state_store may enforce that a run is registered before
    accepting its first checkpoint (e.g. TimescaleLiveStateStore's foreign
    key to backtest_runs). register_run() must therefore run before
    LiveTrader's first internal persist, not after build_live_trader()
    returns (issue #90) — MemoryLiveStateStore doesn't enforce this, so it
    can't catch a regression here; this fake does."""
    config = make_test_cfg(mode="sim")
    registered_run_ids: set[str] = set()

    class _RunMustBeRegisteredFirstStore:
        def load(self, state_key):
            return None

        def save(self, state, orders=()):
            if state.run_id not in registered_run_ids:
                raise RuntimeError(f"checkpoint for unregistered run: {state.run_id}")

        def acquire_lease(self, state_key):
            return True

        def release_lease(self, state_key):
            pass

    callbacks = MagicMock()
    callbacks.register_run.side_effect = registered_run_ids.add

    with (
        patch("librae.orchestration.live._build_adapter", return_value=MagicMock()),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch("librae.orchestration.live._TimescaleCallbacks", return_value=callbacks),
    ):
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            state_store=_RunMustBeRegisteredFirstStore(),
        )

    callbacks.register_run.assert_called_once()


def test_restored_run_also_calls_register_run() -> None:
    """A restarted process must re-sync callback state too, not just skip
    straight to trading — _TimescaleCallbacks caches run_id purely from
    register_run() and has no other way to learn it after a restart, since
    on_position_event/on_financing_cash_flow/on_runtime_event all read that
    cached value rather than receiving run_id as an argument. Without this,
    every DB write after a restart silently fails on the run_id foreign
    key (caught by _write's best-effort try/except) — invisible short of
    reading warning logs."""
    config = make_test_cfg(mode="sim")
    store = MemoryLiveStateStore()

    first_callbacks = MagicMock()
    with (
        patch("librae.orchestration.live._build_adapter", return_value=MagicMock()),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch("librae.orchestration.live._TimescaleCallbacks", return_value=first_callbacks),
    ):
        first = build_live_trader(
            MagicMock(), lambda frame: frame, config=config, state_store=store
        )

    second_callbacks = MagicMock()
    with (
        patch("librae.orchestration.live._build_adapter", return_value=MagicMock()),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch("librae.orchestration.live._TimescaleCallbacks", return_value=second_callbacks),
    ):
        second = build_live_trader(
            MagicMock(), lambda frame: frame, config=config, state_store=store
        )

    assert second.run_id == first.run_id
    second_callbacks.register_run.assert_called_once_with(second.run_id)


def test_restored_run_registers_before_state_recovered_event() -> None:
    """register_run() must complete before the state_recovered RuntimeEvent
    fires, not just before build_live_trader() returns — _restore_state
    fires state_recovered synchronously as part of restoration itself, so a
    register_run() called only afterward (at the end of __init__) is too
    late: _TimescaleCallbacks.on_runtime_event would still read the stale
    cached run_id and hit a foreign-key violation on every restart."""
    config = make_test_cfg(mode="sim")
    store = MemoryLiveStateStore()
    call_order: list[str] = []

    first_callbacks = MagicMock()
    with (
        patch("librae.orchestration.live._build_adapter", return_value=MagicMock()),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch("librae.orchestration.live._TimescaleCallbacks", return_value=first_callbacks),
    ):
        build_live_trader(MagicMock(), lambda frame: frame, config=config, state_store=store)

    second_callbacks = MagicMock()
    second_callbacks.register_run.side_effect = lambda run_id: call_order.append("register_run")
    second_callbacks.on_runtime_event.side_effect = lambda event: call_order.append(
        f"on_runtime_event:{event.event_type}"
    )
    with (
        patch("librae.orchestration.live._build_adapter", return_value=MagicMock()),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch("librae.orchestration.live._TimescaleCallbacks", return_value=second_callbacks),
    ):
        build_live_trader(MagicMock(), lambda frame: frame, config=config, state_store=store)

    assert call_order == ["register_run", "on_runtime_event:state_recovered"]


def test_database_and_telegram_wiring_are_independent() -> None:
    config = make_test_cfg(mode="sim")
    notifier = MagicMock(enabled=True)

    with (
        patch("librae.orchestration.live._build_adapter", return_value=MagicMock()),
        patch("librae.orchestration.live._build_state_store") as build_state_store,
        patch("librae.orchestration.live._build_notifier", return_value=notifier) as build_notifier,
        patch("librae.orchestration.live._TimescaleCallbacks") as build_callbacks,
    ):
        trader = build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
            telegram_config={"enabled": True},
        )

    build_notifier.assert_called_once_with({"enabled": True})
    build_state_store.assert_not_called()
    build_callbacks.assert_not_called()
    assert trader._notifier is notifier


def test_factory_builds_external_data_adapter() -> None:
    config = make_test_cfg(
        mode="sim",
        data_source="vendor_feed",
        instrument_overrides={
            "BTCUSDT": {
                "data_adapter": "vendor_plugin",
                "currency": "USDT",
                "instrument_type": "spot",
            }
        },
    )
    adapter = MagicMock()
    factory = MagicMock(return_value=adapter)

    trader = build_live_trader(
        MagicMock(),
        lambda frame: frame,
        config=config,
        database_enabled=False,
        adapter_factories={"vendor_plugin": factory},
    )

    factory.assert_called_once_with(trading=False)
    assert trader._fetchers


def test_build_adapter_selects_binanceusdm_for_non_spot_instrument_type() -> None:
    mock_exchange = MagicMock()
    with patch("librae.brokers.crypto_adapter._require_ccxt") as mock_require_ccxt:
        mock_require_ccxt.return_value = MagicMock(
            binanceusdm=MagicMock(return_value=mock_exchange)
        )
        adapter = _build_adapter("crypto", trading=False, instrument_type="contract_perpetual")

    assert adapter._exchange_id == "binanceusdm"


def test_build_adapter_selects_spot_binance_by_default() -> None:
    mock_exchange = MagicMock()
    with patch("librae.brokers.crypto_adapter._require_ccxt") as mock_require_ccxt:
        mock_require_ccxt.return_value = MagicMock(binance=MagicMock(return_value=mock_exchange))
        spot_adapter = _build_adapter("crypto", trading=False, instrument_type="spot")
        omitted_adapter = _build_adapter("crypto", trading=False)

    assert spot_adapter._exchange_id == "binance"
    assert omitted_adapter._exchange_id == "binance"


def test_build_adapter_reads_credentials_even_when_not_trading() -> None:
    """Borrow rates sit behind a signed endpoint, and reading one is not
    placing an order — sim must be able to use credentials the operator
    explicitly supplied, or it silently stops charging shorts."""
    binance = MagicMock(return_value=MagicMock())
    with (
        patch("librae.brokers.crypto_adapter._require_ccxt") as mock_require_ccxt,
        patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "key-from-env", "BINANCE_API_SECRET": "secret-from-env"},
        ),
    ):
        mock_require_ccxt.return_value = MagicMock(binance=binance)
        _build_adapter("crypto", trading=False, instrument_type="spot")

    assert binance.call_args[0][0]["apiKey"] == "key-from-env"


def test_build_adapter_stays_read_only_without_credentials_in_the_environment() -> None:
    binance = MagicMock(return_value=MagicMock())
    with (
        patch("librae.brokers.crypto_adapter._require_ccxt") as mock_require_ccxt,
        patch.dict(os.environ, {}, clear=True),
    ):
        mock_require_ccxt.return_value = MagicMock(binance=binance)
        _build_adapter("crypto", trading=False, instrument_type="spot")

    assert "apiKey" not in binance.call_args[0][0]


def test_data_adapters_are_not_shared_across_differing_instrument_types() -> None:
    """Same data_adapter/data_source but different instrument_type (spot vs
    perpetual) must not reuse one cached adapter instance — they need
    different CCXT exchange ids (see _build_adapter)."""
    config = make_test_cfg(
        mode="sim",
        symbols=["BTCUSDT", "BTCUSDT_PERP"],
        symbol_cost_overrides={"BTCUSDT_PERP": {"multiplier": 1.0}},
        instrument_overrides={
            "BTCUSDT_PERP": {
                "data_adapter": "crypto",
                "data_source": "binance_spot",
                "instrument_type": "contract_perpetual",
                "currency": "USDT",
                "calendar_id": "24/7",
            }
        },
    )
    spot_adapter = MagicMock()
    perp_adapter = MagicMock()

    with patch(
        "librae.orchestration.live._build_adapter",
        side_effect=[spot_adapter, perp_adapter],
    ) as build_adapter:
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
        )

    assert build_adapter.call_count == 2
    instrument_types = {call.kwargs["instrument_type"] for call in build_adapter.call_args_list}
    assert instrument_types == {"spot", "contract_perpetual"}


def test_factory_rejects_different_live_brokers_before_building_adapters() -> None:
    config = make_test_cfg(
        mode="live",
        broker=None,
        symbols=["BTCUSDT", "ETHUSDT"],
        symbol_cost_overrides={"ETHUSDT": {"multiplier": 1.0}},
        instrument_overrides={
            "BTCUSDT": {"broker": "binance"},
            "ETHUSDT": {
                "broker": "ibkr",
                "data_adapter": "crypto",
                "instrument_type": "spot",
                "currency": "USDT",
                "security_type": "STK",
            },
        },
    )

    with (
        patch("librae.orchestration.live._build_adapter") as build_adapter,
        pytest.raises(ValueError, match="requires one execution broker"),
    ):
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
            state_store=MemoryLiveStateStore(),
            runtime_revision="test-runtime",
        )

    build_adapter.assert_not_called()


def test_factory_rejects_mixed_binance_products_before_building_adapters() -> None:
    config = make_test_cfg(
        mode="live",
        broker="binance",
        symbols=["BTCUSDT", "BTCUSDT_PERP"],
        symbol_cost_overrides={"BTCUSDT_PERP": {"multiplier": 1.0}},
        instrument_overrides={
            "BTCUSDT_PERP": {
                "data_adapter": "crypto",
                "data_source": "binance_futures_continuous",
                "venue_symbol": "BTC/USDT:USDT",
                "instrument_type": "contract_perpetual",
                "currency": "USDT",
            }
        },
    )

    with (
        patch("librae.orchestration.live._build_adapter") as build_adapter,
        pytest.raises(ValueError, match="incompatible venues"),
    ):
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
            state_store=MemoryLiveStateStore(),
            runtime_revision="test-runtime",
        )

    build_adapter.assert_not_called()


def test_factory_keys_binance_order_adapter_by_execution_venue() -> None:
    config = make_test_cfg(
        mode="live",
        broker="binance",
        symbols=["BTC_PERP", "ETH_PERP"],
        data_source="vendor_feed",
        symbol_cost_overrides={
            "BTC_PERP": {"multiplier": 1.0},
            "ETH_PERP": {"multiplier": 1.0},
        },
        instrument_overrides={
            symbol: {
                "data_adapter": "vendor_plugin",
                "instrument_type": "contract_perpetual",
                "currency": "USDT",
                "calendar_id": "24/7",
            }
            for symbol in ("BTC_PERP", "ETH_PERP")
        },
    )
    data_adapter = MagicMock()
    order_adapter = MagicMock()

    with patch(
        "librae.orchestration.live._build_adapter",
        side_effect=[data_adapter, order_adapter],
    ) as build_adapter:
        trader = build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
            adapter_factories={"vendor_plugin": MagicMock()},
            state_store=MemoryLiveStateStore(),
            runtime_revision="test-runtime",
        )

    assert build_adapter.call_count == 2
    order_call = build_adapter.call_args_list[1]
    assert order_call.args == ("binance",)
    assert order_call.kwargs["trading"] is True
    assert order_call.kwargs["instrument_type"] == "contract_perpetual"
    assert trader._executor.get_order_adapter("BTC_PERP") is order_adapter
    assert trader._executor.get_order_adapter("ETH_PERP") is order_adapter


def test_live_execution_allows_independent_market_data_sources() -> None:
    config = make_test_cfg(
        mode="live",
        broker="binance",
        symbols=["BTC", "ETH"],
        symbol_cost_overrides={
            "BTC": {"multiplier": 1.0},
            "ETH": {"multiplier": 1.0},
        },
        instrument_overrides={
            "BTC": {
                "data_adapter": "feed_a",
                "data_source": "source_a",
                "instrument_type": "spot",
                "currency": "USDT",
                "calendar_id": "24/7",
            },
            "ETH": {
                "data_adapter": "feed_b",
                "data_source": "source_b",
                "instrument_type": "spot",
                "currency": "USDT",
                "calendar_id": "24/7",
            },
        },
    )
    first_feed = MagicMock()
    second_feed = MagicMock()
    order_adapter = MagicMock()
    factories = {
        "feed_a": MagicMock(return_value=first_feed),
        "feed_b": MagicMock(return_value=second_feed),
        "binance": MagicMock(return_value=order_adapter),
    }

    trader = build_live_trader(
        MagicMock(),
        lambda frame: frame,
        config=config,
        database_enabled=False,
        adapter_factories=factories,
        state_store=MemoryLiveStateStore(),
        runtime_revision="test-runtime",
    )

    factories["feed_a"].assert_called_once_with(trading=False)
    factories["feed_b"].assert_called_once_with(trading=False)
    factories["binance"].assert_called_once_with(trading=True)
    assert trader._executor.get_order_adapter("BTC") is order_adapter
    assert trader._executor.get_order_adapter("ETH") is order_adapter


def test_factory_reuses_external_adapter_for_live_orders() -> None:
    config = make_test_cfg(
        mode="live",
        broker="vendor_plugin",
        data_source="vendor_feed",
        instrument_overrides={
            "BTCUSDT": {
                "data_adapter": "vendor_plugin",
                "currency": "USDT",
                "instrument_type": "spot",
            }
        },
    )
    adapter = MagicMock()
    factory = MagicMock(return_value=adapter)

    trader = build_live_trader(
        MagicMock(),
        lambda frame: frame,
        config=config,
        database_enabled=False,
        adapter_factories={"vendor_plugin": factory},
        state_store=MemoryLiveStateStore(),
        runtime_revision="test-runtime",
    )

    factory.assert_called_once_with(trading=True)
    assert trader._executor.get_order_adapter("BTCUSDT") is adapter


def test_data_adapter_overrides_injects_per_symbol_instance_directly() -> None:
    config = make_test_cfg(mode="sim")
    override_adapter = MagicMock()
    override_adapter.fetch_ohlcv.return_value = pd.DataFrame(
        columns=["ts", "open", "high", "low", "close", "volume"]
    )

    with patch("librae.orchestration.live._build_adapter") as build_adapter:
        trader = build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
            data_adapter_overrides={"BTCUSDT": override_adapter},
        )

    build_adapter.assert_not_called()
    trader._fetchers["BTCUSDT"]("BTCUSDT", "1h", 10)
    override_adapter.fetch_ohlcv.assert_called_once()


def test_data_adapter_overrides_rejects_unknown_symbol() -> None:
    config = make_test_cfg(mode="sim")

    with (
        patch("librae.orchestration.live._build_adapter") as build_adapter,
        pytest.raises(ValueError, match="unknown symbols"),
    ):
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
            data_adapter_overrides={"NOT_A_SYMBOL": MagicMock()},
        )

    build_adapter.assert_not_called()


def test_factory_rejects_missing_live_revision_before_building_adapters() -> None:
    config = make_test_cfg(mode="live")

    with (
        patch("librae.orchestration.live._build_adapter") as build_adapter,
        pytest.raises(ValueError, match="runtime_revision"),
    ):
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            state_store=MemoryLiveStateStore(),
        )

    build_adapter.assert_not_called()


def test_factory_accepts_injected_notifier_and_state_store() -> None:
    config = make_test_cfg(mode="sim")
    adapter = MagicMock()
    notifier = MagicMock(enabled=True)
    state_store = MemoryLiveStateStore()

    trader = build_live_trader(
        MagicMock(),
        lambda frame: frame,
        config=config,
        database_enabled=False,
        adapter_factories={"crypto": MagicMock(return_value=adapter)},
        notifier=notifier,
        status_interval_periods=5,
        state_store=state_store,
    )

    assert trader._notifier is notifier
    assert trader._state_store is state_store
    assert trader._status_interval == 5


def test_factory_rejects_two_notifier_sources() -> None:
    config = make_test_cfg(mode="sim")

    with pytest.raises(ValueError, match="notifier or configure Telegram"):
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
            notifier=MagicMock(),
            telegram_config={"enabled": True},
        )


def test_timescale_callbacks_writes_trade_event() -> None:
    """asdict(PositionEvent) must match write_trade_event's real signature —
    autospec enforces this; a plain MagicMock would hide a mismatch since
    _write() swallows the resulting TypeError as a logged DB failure."""
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, None)
    callbacks._run_id = "run-1"
    ts = datetime.now(UTC)

    from librae.core.executor import PositionEvent

    event = PositionEvent(
        ts=ts,
        symbol="BTCUSDT",
        side="long",
        event_type="close",
        fill_quantity=1.0,
        price=100.0,
        entry_price=90.0,
        remaining_quantity=0.0,
        notional=100.0,
        commission=0.1,
        slippage=0.05,
        tax=0.0,
        realized_pnl=10.0,
        net_return=11.1,
        entry_at=ts,
        periods_held=3,
        reason="take_profit",
        entry_commission=0.1,
        entry_slippage=0.05,
        entry_tax=0.0,
        group_id="grp-1",
        time_in_force="day",
        margin_locked=0.0,
        leverage=None,
        liquidation_price=None,
        margin_roi=11.1,
    )

    with patch("librae.db.timescale_writer.write_trade_event", autospec=True) as write:
        callbacks.on_position_event(event, sequence=1)

    assert callbacks._failures.get("write_trade_event", 0) == 0
    write.assert_called_once()
    assert write.call_args.kwargs["group_id"] == "grp-1"
    assert write.call_args.kwargs["time_in_force"] == "day"
    assert write.call_args.kwargs["margin_roi"] == 11.1


def test_register_run_seeds_zero_baseline_strategy_performance() -> None:
    """Without this seed row, strategy_performance has no row at all until
    the first close/reduce (on_performance is dirty-flag gated — see
    live/engine.py), so Total Return/Max Drawdown/Sharpe show "No data"
    next to an already-live Unrealized P&L. register_run() must seed a
    $0/0.0% baseline instead."""
    config = make_test_cfg(mode="sim", initial_balance=50_000.0)
    callbacks = _TimescaleCallbacks(
        config,
        {"BTCUSDT": resolve_symbol(config, "BTCUSDT")},
        None,
    )

    with (
        patch("librae.db.timescale_writer.write_run_metadata", autospec=True),
        patch("librae.db.timescale_writer.write_strategy_performance", autospec=True) as write_perf,
    ):
        callbacks.register_run("run-1")

    write_perf.assert_called_once()
    kwargs = write_perf.call_args.kwargs
    assert kwargs["run_id"] == "run-1"
    assert kwargs["initial_cash"] == kwargs["final_equity"] == 50_000.0
    assert kwargs["net_pnl"] == 0.0
    assert kwargs["metrics"].total_return == 0.0


def test_register_run_persists_poll_seconds_for_runtime_health() -> None:
    config = make_test_cfg(mode="sim", poll_seconds=17, session_mode="regular")
    callbacks = _TimescaleCallbacks(
        config,
        {"BTCUSDT": resolve_symbol(config, "BTCUSDT")},
        None,
    )

    with (
        patch("librae.db.timescale_writer.write_run_metadata", autospec=True) as write_run,
        patch("librae.db.timescale_writer.write_strategy_performance", autospec=True),
    ):
        callbacks.register_run("run-1")

    assert write_run.call_args.kwargs["poll_seconds"] == 17
    assert write_run.call_args.kwargs["session_mode"] == "regular"
    subscription = write_run.call_args.kwargs["primary_subscriptions"][0]
    assert subscription.symbol == "BTCUSDT"
    assert subscription.calendar_id == "24/7"
    assert write_run.call_args.kwargs["config_hash"] == config.config_hash


def test_register_run_persists_resolved_per_symbol_data_sources() -> None:
    config = make_test_cfg(
        data_source="run-default",
        instrument_overrides={
            "BTCUSDT": {
                "data_source": "ibkr",
                "data_adapter": "ibkr",
                "security_type": "STK",
            }
        },
    )
    instruments = {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}
    callbacks = _TimescaleCallbacks(config, instruments, None)

    with (
        patch("librae.db.timescale_writer.write_run_metadata", autospec=True) as write_run,
        patch("librae.db.timescale_writer.write_strategy_performance", autospec=True),
    ):
        callbacks.register_run("run-1")

    assert write_run.call_args.kwargs["data_source"] == "run-default"
    assert write_run.call_args.kwargs["data_source_by_symbol"] == {"BTCUSDT": "ibkr"}


def test_live_ohlcv_write_preserves_session_identity() -> None:
    config = make_test_cfg(mode="sim", session_mode="regular")
    callbacks = _TimescaleCallbacks(
        config,
        {"BTCUSDT": resolve_symbol(config, "BTCUSDT")},
        None,
    )

    with patch("librae.db.timescale_writer.write_ohlcv", autospec=True) as write:
        callbacks.on_ohlcv(
            "BTCUSDT",
            "H1",
            {
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "close": 100.0,
                "volume": 10.0,
                "available_at": datetime(2025, 1, 1, 1, tzinfo=UTC),
            },
            datetime(2025, 1, 1, tzinfo=UTC),
        )

    subscription = write.call_args.args[1]
    assert subscription.to_dict() == {
        "symbol": "BTCUSDT",
        "timeframe": "H1",
        "calendar_id": "24/7",
        "session_mode": "regular",
        "data_source": "binance_spot",
        "instrument_type": "spot",
    }
    assert write.call_args.args[0]["available_at"].iloc[0] == datetime(2025, 1, 1, 1, tzinfo=UTC)


def test_live_ohlcv_analytics_write_remains_best_effort() -> None:
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(
        config,
        {"BTCUSDT": resolve_symbol(config, "BTCUSDT")},
        None,
    )

    with patch(
        "librae.db.timescale_writer.write_ohlcv",
        autospec=True,
        side_effect=RuntimeError("database unavailable"),
    ) as write:
        callbacks.on_ohlcv(
            "BTCUSDT",
            "H1",
            {
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "close": 100.0,
                "volume": 10.0,
                "available_at": datetime(2025, 1, 1, 1, tzinfo=UTC),
            },
            datetime(2025, 1, 1, tzinfo=UTC),
        )

    write.assert_called_once()
    assert callbacks._failures["write_ohlcv"] == 1


def test_timescale_callbacks_writes_runtime_event() -> None:
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, None)
    callbacks._run_id = "run-1"
    ts = datetime.now(UTC)

    with patch("librae.db.timescale_writer.write_runtime_event", autospec=True) as write:
        from librae.core.executor import RuntimeEvent

        callbacks.on_runtime_event(RuntimeEvent(ts=ts, event_type="state_recovered", detail={}))

    write.assert_called_once_with(
        run_id="run-1",
        ts=ts,
        event_type="state_recovered",
        symbol=None,
        detail={},
    )


def test_timescale_callbacks_alert_after_repeated_write_failures() -> None:
    config = make_test_cfg(mode="sim")
    notifier = MagicMock(enabled=True)
    callbacks = _TimescaleCallbacks(
        config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, notifier
    )
    failing_write = MagicMock(side_effect=RuntimeError("db down"))
    failing_write.__name__ = "failing_write"

    for _ in range(3):
        callbacks._write(failing_write)

    notifier.send_alert.assert_called_once()
    assert "DB Write Failing" in notifier.send_alert.call_args.kwargs["title"]


def test_timescale_callbacks_track_failures_per_callback() -> None:
    """A different callback succeeding (e.g. equity_curve writes every bar)
    must not reset another callback's consecutive-failure streak (e.g.
    trade_event writes failing every time) — each is tracked independently."""
    config = make_test_cfg(mode="sim")
    notifier = MagicMock(enabled=True)
    callbacks = _TimescaleCallbacks(
        config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, notifier
    )

    failing_write = MagicMock(side_effect=RuntimeError("db down"))
    failing_write.__name__ = "failing_write"
    succeeding_write = MagicMock()
    succeeding_write.__name__ = "succeeding_write"

    callbacks._write(failing_write)
    callbacks._write(succeeding_write)
    callbacks._write(failing_write)
    callbacks._write(succeeding_write)
    callbacks._write(failing_write)

    notifier.send_alert.assert_called_once()
    assert callbacks._failures["failing_write"] == 3
    assert callbacks._failures["succeeding_write"] == 0


def test_timescale_callbacks_critical_write_alerts_on_first_failure() -> None:
    """Critical event writes alert immediately but remain best-effort."""
    config = make_test_cfg(mode="sim")
    notifier = MagicMock(enabled=True)
    callbacks = _TimescaleCallbacks(
        config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, notifier
    )
    failing_write = MagicMock(side_effect=RuntimeError("db down"))
    failing_write.__name__ = "failing_write"

    callbacks._write(failing_write, critical=True)

    notifier.send_alert.assert_called_once()
    assert callbacks._failures["failing_write"] == 1


def test_run_metadata_failure_stops_startup_and_reports_distinct_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = make_test_cfg(mode="sim")
    notifier = MagicMock(enabled=True)
    callbacks = _TimescaleCallbacks(
        config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, notifier
    )

    with (
        patch(
            "librae.db.timescale_writer.write_run_metadata",
            autospec=True,
            side_effect=RuntimeError("schema is stale"),
        ),
        patch("librae.db.timescale_writer.write_strategy_performance", autospec=True) as write_perf,
        caplog.at_level("ERROR", logger="librae.orchestration.live"),
        pytest.raises(RuntimeError, match=r"required run metadata.*trading did not start"),
    ):
        callbacks.register_run("run-1")

    write_perf.assert_not_called()
    notifier.send_alert.assert_called_once()
    assert "DB Startup Failed" in notifier.send_alert.call_args.kwargs["title"]
    assert "trading did not start" in notifier.send_alert.call_args.kwargs["message"]
    assert "trading will not start" in caplog.text


@pytest.mark.parametrize("notification_failure", ["enabled", "send"])
def test_notification_failure_cannot_mask_run_metadata_failure(
    notification_failure: str,
) -> None:
    class FailingNotifier:
        @property
        def enabled(self) -> bool:
            if notification_failure == "enabled":
                raise RuntimeError("enabled unavailable")
            return True

        def send_alert(self, **_kwargs: object) -> None:
            raise RuntimeError("send unavailable")

    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(
        config,
        {"BTCUSDT": resolve_symbol(config, "BTCUSDT")},
        FailingNotifier(),
    )
    database_error = RuntimeError("schema is stale")

    with (
        patch(
            "librae.db.timescale_writer.write_run_metadata",
            autospec=True,
            side_effect=database_error,
        ),
        patch("librae.db.timescale_writer.write_strategy_performance", autospec=True),
        pytest.raises(RuntimeError, match="required run metadata") as error,
    ):
        callbacks.register_run("run-1")

    assert error.value.__cause__ is database_error


def test_reference_factory_does_not_return_when_run_metadata_parent_is_missing() -> None:
    config = make_test_cfg(mode="sim")
    adapter = MagicMock()

    with (
        patch("librae.orchestration.live._build_adapter", return_value=adapter),
        patch("librae.orchestration.live._build_notifier", return_value=None),
        patch(
            "librae.db.timescale_writer.write_run_metadata",
            autospec=True,
            side_effect=RuntimeError("missing schema column"),
        ),
        patch("librae.db.timescale_writer.write_strategy_performance", autospec=True) as write_perf,
        pytest.raises(RuntimeError, match=r"required run metadata.*trading did not start"),
    ):
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            state_store=MemoryLiveStateStore(),
        )

    write_perf.assert_not_called()
    adapter.fetch_ohlcv.assert_not_called()


def test_timescale_callbacks_mark_financing_writes_critical() -> None:
    """Irreplaceable event writes must request immediate alerts."""
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, None)
    callbacks._run_id = "run-1"

    with patch.object(callbacks, "_write") as write:
        from librae.core.financing import FinancingCashFlow

        callbacks.on_financing_cash_flow(
            FinancingCashFlow(
                ts=datetime.now(UTC),
                symbol="BTCUSDT",
                side="long",
                quantity=1.0,
                mark_price=100.0,
                multiplier=1.0,
                rate=0.0001,
                cash_flow=-0.01,
                group_id=None,
                entry_at=datetime.now(UTC),
            )
        )
    matching = [
        call
        for call in write.call_args_list
        if call.args[0].__name__ == "write_financing_cash_flow"
    ]
    assert len(matching) == 1
    assert matching[0].kwargs["critical"] is True


def test_timescale_callbacks_on_position_event_marks_write_trade_event_critical() -> None:
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, None)
    callbacks._run_id = "run-1"

    from librae.core.executor import PositionEvent

    ts = datetime.now(UTC)
    event = PositionEvent(
        ts=ts,
        symbol="BTCUSDT",
        side="long",
        event_type="open",
        fill_quantity=1.0,
        price=100.0,
        entry_price=100.0,
        remaining_quantity=1.0,
        notional=100.0,
        commission=0.1,
        slippage=0.05,
        tax=0.0,
        realized_pnl=None,
        net_return=None,
        entry_at=ts,
        periods_held=0,
        reason="entry_signal",
        entry_commission=0.1,
        entry_slippage=0.05,
        entry_tax=0.0,
        group_id=None,
        time_in_force="day",
        margin_locked=100.0,
        leverage=1.0,
        liquidation_price=None,
        margin_roi=None,
    )

    with patch.object(callbacks, "_write") as write:
        callbacks.on_position_event(event, sequence=1)

    assert write.call_args.args[0].__name__ == "write_trade_event"
    assert write.call_args.kwargs["critical"] is True
