"""A live halt holds across restarts the state store did not see, until an operator resets it.

A halt is written to the halt file before it acts, so a restart before the
store records it comes up halted and sends nothing, whatever the older
checkpoint says. Only ``reset_halt`` removes the file. A halt file that cannot
be written holds nothing back: the halt still cancels and says so. Sim has no
halt file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from librae.live.engine import LiveTrader
from librae.live.state import MemoryLiveStateStore, StateStoreUnavailableError

from tests.engine.live_factories import (
    _make_ohlcv_df,
    _simple_feature_fn,
    _test_cfg,
    _test_execution_identity,
    _zero_cost_model,
    halt_file_of,
)
from tests.engine.test_live_cycle_safety import (
    _alerts_titled,
    _CountingStrategy,
    _order,
    _resting_trader,
    _store_down,
    _successor,
    _venue_positions,
)
from tests.engine.test_live_flatten import SYMBOLS, _adapter

_VENUE_UNCHANGED = {symbol: 1.0 for symbol in SYMBOLS}


def _flat(trader) -> None:
    """Hold no position, so a reset needs no valuation mark."""
    trader._positions = {}
    trader._cash = trader._equity_peak = 300.0


def _halted_in_outage(store: MemoryLiveStateStore, *, flat: bool = False):
    """A trader halted while the store is down, holding a working order and a queued, unsent one."""
    trader, adapter, _, events, alerts = _resting_trader(close=100.0, store=store)
    if flat:
        _flat(trader)
    trader._last_reconciliation_at = trader._utc_now()
    trader._active_orders.append(_order("q-2", "BBB"))
    trader._persist_state()
    outage = _store_down(store)
    trader.request_halt("desk asked to stop")
    with pytest.raises(StateStoreUnavailableError):
        trader._poll_cycle()
    return trader, adapter, outage, events, alerts


def _restart(adapter, store: MemoryLiveStateStore, venue: dict[str, float] = _VENUE_UNCHANGED):
    """Start a successor on ``store`` once it answers, the venue confirming the halt's cancel."""
    adapter.get_order.return_value = adapter.cancel_order.return_value
    adapter.get_position.side_effect = _venue_positions(venue)
    adapter.place_order.reset_mock()
    adapter.cancel_order.reset_mock()
    successor, alerts = _successor(adapter, store, close=100.0)
    successor._initialize_run()
    return successor, alerts


class TestARestartBeforeTheStoreRecordsTheHalt:
    def test_comes_up_halted_and_sends_nothing(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, outage, _, alerts = _halted_in_outage(store)
        [halt] = _alerts_titled(alerts, "Manual Halt")
        assert "not recorded yet (StateStoreUnavailableError" in halt["message"]
        assert "resume trading unhalted" not in halt["message"]
        assert store.load(trader._state_key).halted is False, "the store never saw the halt"
        outage["down"] = False

        successor, successor_alerts = _restart(adapter, store)
        successor._poll_cycle()

        assert successor._halted is True
        assert store.load(successor._state_key).halted is True
        assert successor._active_orders == []
        assert store.orders["q-2"].status == "cancelled"
        adapter.place_order.assert_not_called()
        [restored] = _alerts_titled(successor_alerts, "Halt Restored")
        assert "Manual Halt: desk asked to stop" in restored["message"]
        assert halt_file_of(store).exists()

    def test_a_reset_clears_it_and_trading_resumes(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        _, adapter, outage, _, _ = _halted_in_outage(store, flat=True)
        outage["down"] = False
        successor, _ = _restart(adapter, store, venue={})
        successor._poll_cycle()
        assert successor._halted is True

        successor.reset_halt()

        assert successor._halted is False
        assert not halt_file_of(store).exists()
        assert store.load(successor._state_key).halted is False
        strategy = successor._strategy
        successor._poll_cycle()
        assert strategy.calls == 1, "decides again"
        successor._release_lease()
        third, alerts = _restart(adapter, store, venue={})
        assert third._halted is False
        assert _alerts_titled(alerts, "Halt Restored") == []

    def test_a_start_that_cannot_record_it_fails_before_any_order_work(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        _, adapter, outage, _, _ = _halted_in_outage(store)
        outage["down"] = False
        for call in (adapter.place_order, adapter.cancel_order, adapter.get_order):
            call.reset_mock()
        successor, _ = _successor(adapter, store, close=100.0)
        _store_down(store)

        with pytest.raises(StateStoreUnavailableError):
            successor._initialize_run()

        adapter.place_order.assert_not_called()
        adapter.cancel_order.assert_not_called()
        adapter.get_order.assert_not_called()
        assert halt_file_of(store).exists()


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe not utf-8", b"{not json", b'["a list"]'],
    ids=["undecodable", "unparsable", "not-a-record"],
)
def test_a_halt_file_that_does_not_read_as_a_record_still_halts_the_start(content):
    store = MemoryLiveStateStore(restart_durable_for_tests=True)
    trader, adapter, _, _, _ = _resting_trader(close=100.0, store=store)
    trader._persist_state()
    halt_file_of(store).write_bytes(content)

    successor, alerts = _restart(adapter, store)
    successor._poll_cycle()

    assert successor._halted is True
    adapter.place_order.assert_not_called()
    [restored] = _alerts_titled(alerts, "Halt Restored")
    assert "unreadable halt record" in restored["message"]


class TestAHaltWithTheStoreUp:
    def test_is_recorded_in_both_and_restarts_halted_without_restoring_it(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, alerts = _resting_trader(close=100.0, store=store)
        trader._last_reconciliation_at = trader._utc_now()
        trader.request_halt("desk asked to stop")

        trader._poll_cycle()

        [halt] = _alerts_titled(alerts, "Manual Halt")
        assert halt["message"] == "desk asked to stop; trading halted."
        assert adapter.cancel_order.call_count == 1
        assert store.load(trader._state_key).halted is True
        assert halt_file_of(store).exists()
        successor, successor_alerts = _restart(adapter, store)
        assert successor._halted is True
        assert _alerts_titled(successor_alerts, "Halt Restored") == []


class TestAHaltFileThatCannotBeWritten:
    @pytest.mark.parametrize("store_down", [False, True], ids=["store-up", "store-down"])
    def test_still_cancels_and_says_so(self, monkeypatch, store_down):
        def refuse(*_args):
            raise OSError("No space left on device")

        monkeypatch.setattr("librae.live.engine.write_halt_file", refuse)
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, alerts = _resting_trader(close=100.0, store=store)
        trader._last_reconciliation_at = trader._utc_now()
        trader._persist_state()
        if store_down:
            _store_down(store)
        trader.request_halt("desk asked to stop")

        if store_down:
            with pytest.raises(StateStoreUnavailableError):
                trader._poll_cycle()
        else:
            trader._poll_cycle()

        assert trader._halted is True
        assert adapter.cancel_order.call_count == 1
        [halt] = _alerts_titled(alerts, "Manual Halt")
        assert "halt file not written (OSError: No space left on device)" in halt["message"]
        restart_warning = "so a restart now would resume trading unhalted"
        assert (restart_warning in halt["message"]) is store_down
        assert not halt_file_of(store).exists()

    def test_a_flatten_still_closes_the_book_and_says_so(self, monkeypatch):
        def refuse(*_args):
            raise OSError("Read-only file system")

        monkeypatch.setattr("librae.live.engine.write_halt_file", refuse)
        trader, adapter, _, _, alerts = _resting_trader(close=50.0)
        trader._active_orders = []
        trader._last_reconciliation_at = trader._utc_now()

        trader._poll_cycle()

        assert trader._halted is True
        assert adapter.place_order.call_count == len(SYMBOLS)
        [breach] = _alerts_titled(alerts, "Max Drawdown Breach")
        assert "halt file not written (OSError: Read-only file system)" in breach["message"]


def test_a_reset_that_cannot_remove_the_halt_file_changes_nothing(monkeypatch):
    store = MemoryLiveStateStore(restart_durable_for_tests=True)
    trader, _, _, _, _ = _resting_trader(close=100.0, store=store)
    _flat(trader)
    trader._active_orders = []
    trader._last_reconciliation_at = trader._utc_now()
    trader.halt("desk asked to stop")

    def refuse(*_args, **_kwargs):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(Path, "unlink", refuse)
    with pytest.raises(PermissionError):
        trader.reset_halt()

    assert trader._halted is True
    assert store.load(trader._state_key).halted is True
    assert halt_file_of(store).exists()


def _bare_trader(mode: str, **kwargs) -> LiveTrader:
    adapter = None
    if mode == "live":
        adapter = _adapter(below_minimum="")
        adapter.execution_identity = _test_execution_identity
    return LiveTrader(
        _CountingStrategy(),
        _simple_feature_fn,
        config=_test_cfg(mode=mode, symbols=SYMBOLS),
        adapter=lambda *a, **kw: _make_ohlcv_df(),
        order_adapter=adapter,
        cost_model=_zero_cost_model(),
        state_store=MemoryLiveStateStore(restart_durable_for_tests=mode == "live"),
        runtime_revision="test-runtime" if mode == "live" else None,
        on_ohlcv=None,
        **kwargs,
    )


class TestConstruction:
    def test_live_requires_a_halt_file(self):
        with pytest.raises(ValueError, match="live mode requires halt_file"):
            _bare_trader("live")

    def test_live_refuses_a_halt_file_in_a_missing_directory(self, tmp_path):
        with pytest.raises(ValueError, match="must exist and be writable"):
            _bare_trader("live", halt_file=tmp_path / "absent" / "halt")

    def test_sim_refuses_one_and_halts_as_before(self, tmp_path):
        with pytest.raises(ValueError, match="halt_file is only valid in live mode"):
            _bare_trader("sim", halt_file=tmp_path / "halt")
        trader = _bare_trader("sim")

        trader.halt("desk asked to stop")

        assert trader._halted is True
        assert trader._halt_file is None
