"""Tests for repository-level sim/live integration wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from librae.live.state import MemoryLiveStateStore
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
    on_order_event/on_funding_cash_flow/on_runtime_event all read that
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

    with pytest.raises(ValueError, match="unknown symbols"):
        build_live_trader(
            MagicMock(),
            lambda frame: frame,
            config=config,
            database_enabled=False,
            data_adapter_overrides={"NOT_A_SYMBOL": MagicMock()},
        )


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
    """asdict(OrderEvent) must match write_trade_event's real signature —
    autospec enforces this; a plain MagicMock would hide a mismatch since
    _write() swallows the resulting TypeError as a logged DB failure."""
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(config, {}, None)
    callbacks._run_id = "run-1"
    ts = datetime.now(UTC)

    from librae.core.executor import OrderEvent

    event = OrderEvent(
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
        pnl=10.0,
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
        callbacks.on_order_event(event, sequence=1)

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
    callbacks = _TimescaleCallbacks(config, {}, None)

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


def test_timescale_callbacks_writes_runtime_event() -> None:
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(config, {}, None)
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
    callbacks = _TimescaleCallbacks(config, {}, notifier)
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
    callbacks = _TimescaleCallbacks(config, {}, notifier)

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
    """critical=True writes (register_run's write_run_metadata — fires once
    per process, so it could never reach a consecutive-failure threshold at
    all; trade/funding writes — per-event, irreplaceable financial records,
    unlike a recoverable next-bar snapshot like equity_curve) must alert
    immediately instead of waiting for _DB_FAILURE_ALERT_THRESHOLD."""
    config = make_test_cfg(mode="sim")
    notifier = MagicMock(enabled=True)
    callbacks = _TimescaleCallbacks(config, {}, notifier)
    failing_write = MagicMock(side_effect=RuntimeError("db down"))
    failing_write.__name__ = "failing_write"

    callbacks._write(failing_write, critical=True)

    notifier.send_alert.assert_called_once()
    assert callbacks._failures["failing_write"] == 1


@pytest.mark.parametrize(
    ("method", "callback_name", "call_kwargs"),
    [
        ("register_run", "write_run_metadata", {"run_id": "run-1"}),
        ("on_funding_cash_flow", "write_funding_cash_flow", {}),
    ],
)
def test_timescale_callbacks_mark_one_shot_writes_critical(
    method: str, callback_name: str, call_kwargs: dict
) -> None:
    """Wiring check: register_run and on_funding_cash_flow must pass
    critical=True through to _write, or the first-failure alert silently
    stops firing for these irreplaceable writes."""
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(config, {}, None)
    callbacks._run_id = "run-1"

    with patch.object(callbacks, "_write") as write:
        if method == "on_funding_cash_flow":
            from librae.core.funding import FundingCashFlow

            getattr(callbacks, method)(
                FundingCashFlow(
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
        else:
            getattr(callbacks, method)(**call_kwargs)

    # register_run also seeds a $0/0.0% strategy_performance baseline (a
    # second, equally one-shot _write call) — match by name instead of
    # asserting on the last call, so this doesn't depend on call order.
    matching = [call for call in write.call_args_list if call.args[0].__name__ == callback_name]
    assert len(matching) == 1
    assert matching[0].kwargs["critical"] is True


def test_timescale_callbacks_on_order_event_marks_write_trade_event_critical() -> None:
    config = make_test_cfg(mode="sim")
    callbacks = _TimescaleCallbacks(config, {}, None)
    callbacks._run_id = "run-1"

    from librae.core.executor import OrderEvent

    ts = datetime.now(UTC)
    event = OrderEvent(
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
        pnl=None,
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
        callbacks.on_order_event(event, sequence=1)

    assert write.call_args.args[0].__name__ == "write_trade_event"
    assert write.call_args.kwargs["critical"] is True
