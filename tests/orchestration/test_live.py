"""Tests for repository-level sim/live integration wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from librae.live.state import MemoryLiveStateStore
from tests.conftest import make_test_cfg

from orchestration.live import (
    _status_interval_periods,
    _TimescaleCallbacks,
    build_live_trader,
)


def test_disabled_notifier_does_not_load_optional_integration() -> None:
    with patch.dict("sys.modules", {"notifications.telegram": None}):
        from orchestration.live import _build_notifier

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


def test_missing_db_dependency_is_reported_by_deployment_factory() -> None:
    config = make_test_cfg(mode="sim")

    with (
        patch("orchestration.live._build_adapter", return_value=MagicMock()),
        patch("orchestration.live._build_notifier", return_value=None),
        patch.dict("sys.modules", {"db.timescale_state": None}),
        pytest.raises(ModuleNotFoundError, match="Install Librae's 'db' extra"),
    ):
        build_live_trader(MagicMock(), lambda frame: frame, config=config)


def test_factory_registers_timescale_callbacks() -> None:
    config = make_test_cfg(mode="sim")
    callbacks = MagicMock()

    with (
        patch("orchestration.live._build_adapter", return_value=MagicMock()),
        patch(
            "orchestration.live._build_state_store",
            return_value=MemoryLiveStateStore(),
        ),
        patch("orchestration.live._build_notifier", return_value=None),
        patch("orchestration.live._TimescaleCallbacks", return_value=callbacks),
    ):
        trader = build_live_trader(MagicMock(), lambda frame: frame, config=config)

    callbacks.register_run.assert_called_once_with(trader.run_id)


def test_database_and_telegram_wiring_are_independent() -> None:
    config = make_test_cfg(mode="sim")
    notifier = MagicMock(enabled=True)

    with (
        patch("orchestration.live._build_adapter", return_value=MagicMock()),
        patch("orchestration.live._build_state_store") as build_state_store,
        patch("orchestration.live._build_notifier", return_value=notifier) as build_notifier,
        patch("orchestration.live._TimescaleCallbacks") as build_callbacks,
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
    )

    factory.assert_called_once_with(trading=True)
    assert trader._executor.get_order_adapter("BTCUSDT") is adapter


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
