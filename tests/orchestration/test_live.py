"""Tests for repository-level sim/live integration wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from librae.live.state import MemoryLiveStateStore
from librae.orchestration.live import (
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
