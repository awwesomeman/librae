"""Tests for repository-level sim/live integration wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from librae.live.state import MemoryLiveStateStore
from tests.conftest import make_test_cfg

from orchestration.live import _TimescaleCallbacks, build_live_trader


def test_missing_db_dependency_is_reported_by_deployment_factory() -> None:
    config = make_test_cfg(mode="sim", no_db=False)

    with (
        patch("orchestration.live._build_adapter", return_value=MagicMock()),
        patch("orchestration.live._build_notifier", return_value=None),
        patch.dict("sys.modules", {"db.timescale_state": None}),
        pytest.raises(ModuleNotFoundError, match="Install Librae's 'db' extra"),
    ):
        build_live_trader(MagicMock(), lambda frame: frame, config=config)


def test_factory_registers_timescale_callbacks() -> None:
    config = make_test_cfg(mode="sim", no_db=False)
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


def test_timescale_callbacks_alert_after_repeated_write_failures() -> None:
    config = make_test_cfg(mode="sim", no_db=False)
    notifier = MagicMock(enabled=True)
    callbacks = _TimescaleCallbacks(config, {}, notifier)
    failing_write = MagicMock(side_effect=RuntimeError("db down"))
    failing_write.__name__ = "failing_write"

    for _ in range(3):
        callbacks._write(failing_write)

    notifier.send_alert.assert_called_once()
    assert "DB Write Failing" in notifier.send_alert.call_args.kwargs["title"]
