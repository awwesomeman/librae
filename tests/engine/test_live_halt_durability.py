"""A halt whose checkpoint cannot be written must still reach the operator.

The run stays halted in memory, retries the checkpoint first thing in every
later cycle, and refuses ``reset_halt()`` until the store has recorded the
halt, so nobody resumes from a state the store never saw. An outage fails
every write, so the tests do too unless they pin a narrower path.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from librae.core.strategy import OrderIntent
from librae.live.executor import OrderRequest
from librae.live.state import TrackedOrder

from tests.engine.test_live_flatten import (
    SYMBOLS,
    _adapter,
    _bars,
    _submitted,
    _titled,
    _trader,
)
from tests.engine.test_live_operator_halt import _resting
from tests.engine.test_live_runner import TEST_CLOCK_NOW

OUTAGE = "state store unavailable"


def _store_outage(trader, calls: list[str] | None = None, *, halted_only: bool = False):
    """Fail every save while ``outage['down']`` is set, or only halted ones."""
    store = trader._state_store
    save = store.save
    outage = {"down": True}

    def guarded_save(state, orders=()):
        if calls is not None:
            calls.append("save")
        if outage["down"] and (state.halted or not halted_only):
            raise OSError(OUTAGE)
        save(state, orders)

    store.save = guarded_save
    return outage


def _resting_adapter(calls: list[str] | None = None):
    adapter = _adapter(below_minimum="")

    def record(name, result):
        def call(*args):
            if calls is not None:
                calls.append(name)
            return result(*args)

        return call

    adapter.place_order.side_effect = record(
        "place_order", lambda signal: _resting(signal["client_order_id"])
    )
    adapter.get_order.side_effect = record("get_order", lambda order_id, _s: _resting(order_id))
    adapter.cancel_order.side_effect = record(
        "cancel_order", lambda order_id, _s: _resting(order_id, "canceled")
    )
    return adapter


def _buy_two(trader) -> None:
    """Leave AAA resting at the venue and BBB queued behind it."""
    trader._execute_live_decision(
        [
            OrderIntent(action="long", symbol="AAA", quantity=1.0),
            OrderIntent(action="long", symbol="BBB", quantity=1.0),
        ],
        _bars(),
        TEST_CLOCK_NOW,
    )
    assert len(trader._active_orders) == 2


def _halt_by(trader, adapter, path: str) -> None:
    if path == "halt":
        trader.halt("desk asked to stop")
    elif path == "request_halt":
        trader.request_halt("desk asked to stop")
        trader._poll_cycle()
    else:
        adapter.prepare_order.side_effect = ValueError("AAA quantity exceeds maximum")
        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )


def _halt_alerts(alerts) -> list[dict]:
    return [alert for alert in alerts if "Halt Persisted" not in alert["title"]]


class TestHaltCheckpointFailure:
    @pytest.mark.parametrize(
        ("path", "title"),
        [
            ("halt", "Manual Halt"),
            ("request_halt", "Manual Halt"),
            ("preflight", "Live Order Preflight Rejected"),
        ],
    )
    def test_alerts_naming_the_failure_and_stays_halted(self, path, title):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        _store_outage(trader)

        with pytest.raises(OSError, match=OUTAGE):
            _halt_by(trader, adapter, path)

        assert trader._halted is True
        [halt] = _titled(alerts, title)
        assert "not persisted" in halt["message"]
        assert OUTAGE in halt["message"]
        adapter.place_order.assert_not_called()

    def test_next_cycle_retries_before_any_order_work(self):
        calls: list[str] = []
        adapter = _resting_adapter(calls)
        trader, _, alerts = _trader(adapter)
        _buy_two(trader)
        outage = _store_outage(trader, calls)
        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")
        adapter.cancel_order.assert_not_called()

        calls.clear()
        with pytest.raises(OSError, match=OUTAGE):
            trader._poll_cycle()
        assert calls == ["save"], "no order work while the halt is not persisted"
        assert _titled(alerts, "Halt Persisted") == []

        outage["down"] = False
        calls.clear()
        trader._poll_cycle()
        trader._poll_cycle()

        assert calls[0] == "save"
        assert trader._halted is True
        assert trader._active_orders == [], "the halt's cancellation is finished"
        assert adapter.cancel_order.call_count == 1
        adapter.place_order.assert_called_once()
        assert len(_titled(alerts, "Halt Persisted")) == 1
        assert trader.halt_reset_readiness().reason != "halt_not_persisted"

    def test_reset_is_refused_until_the_halt_is_persisted(self):
        trader, events, _ = _trader(_resting_adapter())
        trader._positions = {}
        outage = _store_outage(trader)
        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")

        readiness = trader.halt_reset_readiness()
        assert readiness.ready is False
        assert readiness.reason == "halt_not_persisted"
        with pytest.raises(RuntimeError, match="halt_not_persisted"):
            trader.reset_halt()
        assert trader._halted is True
        [refusal] = [event for event in events if event.detail.get("cause")]
        assert refusal.detail["cause"] == "halt_not_persisted"

        outage["down"] = False
        trader._poll_cycle()
        trader.reset_halt()

        assert trader._halted is False

    def test_a_repeat_halt_raises_until_the_halt_is_persisted(self):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        _buy_two(trader)
        outage = _store_outage(trader)
        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")

        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked again")
        assert not any("already halted" in alert["message"] for alert in alerts)

        outage["down"] = False
        trader.halt("desk asked a third time")

        assert len(_titled(alerts, "Halt Persisted")) == 1
        assert trader._active_orders == []
        assert trader.halt_reset_readiness().reason != "halt_not_persisted"

    def test_a_reconciliation_halt_alerts_once(self):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        trader._last_reconciliation_at = None
        _store_outage(trader)

        with pytest.raises(OSError, match=OUTAGE):
            trader._maybe_reconcile_runtime()

        [halt] = _halt_alerts(alerts)
        assert "Periodic Position Reconciliation Mismatch" in halt["title"]
        assert "not persisted" in halt["message"]

    def test_a_startup_reconciliation_halt_alerts_once(self):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        request = OrderRequest(
            client_order_id="restored-AAA",
            symbol="AAA",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=TEST_CLOCK_NOW,
        )
        trader._active_orders = [
            TrackedOrder(
                request=request,
                placement_attempted=True,
                placement_attempted_at=TEST_CLOCK_NOW - timedelta(minutes=1),
            )
        ]
        _store_outage(trader)

        with pytest.raises(OSError, match=OUTAGE):
            trader._initialize_run()

        [halt] = _halt_alerts(alerts)
        assert "Ambiguous Restored Order" in halt["title"]

    def test_a_cancellation_failure_is_not_reported_as_unpersisted(self):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        _buy_two(trader)
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")

        with pytest.raises(ValueError, match="order id changed"):
            trader.halt("desk asked to stop")

        assert trader._state_store.load(trader._state_key).halted is True
        assert trader.halt_reset_readiness().reason != "halt_not_persisted"
        [halt] = _titled(alerts, "Manual Halt")
        assert "not persisted" not in halt["message"]
        assert "cancelling tracked orders failed" in halt["message"]


class TestFlattenCheckpointFailure:
    def test_an_outage_still_halts_and_alerts_then_the_exits_go_out(self):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        outage = _store_outage(trader)

        trader.request_flatten("desk asked to close")
        with pytest.raises(OSError, match=OUTAGE):
            trader._poll_cycle()

        assert trader._halted is True
        adapter.place_order.assert_not_called()
        [flatten] = _titled(alerts, "Operator Flatten")
        assert "exit submission failed" in flatten["message"]
        assert "not persisted" in flatten["message"]
        assert trader.halt_reset_readiness().reason == "halt_not_persisted"

        outage["down"] = False
        trader._poll_cycle()

        adapter.cancel_order.assert_not_called()
        assert _submitted(adapter) == [SYMBOLS[0]]
        assert len(trader._active_orders) == len(SYMBOLS)
        assert len(_titled(alerts, "Halt Persisted")) == 1

    def test_a_failed_halt_checkpoint_keeps_the_submitted_exits(self):
        calls: list[str] = []
        adapter = _resting_adapter(calls)
        trader, _, alerts = _trader(adapter)
        outage = _store_outage(trader, calls, halted_only=True)

        trader.request_flatten("desk asked to close")
        with pytest.raises(OSError, match=OUTAGE):
            trader._poll_cycle()

        exits = list(trader._active_orders)
        assert len(exits) == len(SYMBOLS)
        [flatten] = _titled(alerts, "Operator Flatten")
        assert "flattening in progress" in flatten["message"]
        assert "not persisted" in flatten["message"]

        calls.clear()
        with pytest.raises(OSError, match=OUTAGE):
            trader._poll_cycle()
        assert calls == ["save"], "working exits are not polled before the retry"

        outage["down"] = False
        trader._poll_cycle()

        adapter.cancel_order.assert_not_called()
        assert trader._active_orders == exits
        assert not any(order.cancel_requested for order in exits)
        assert len(_titled(alerts, "Halt Persisted")) == 1

    @pytest.mark.parametrize("store_down", [False, True])
    def test_a_halt_inside_the_flatten_cancels_as_with_the_store_up(self, store_down):
        adapter = _resting_adapter()
        # Placement fails and the client-id lookup finds nothing.
        adapter.place_order.side_effect = lambda _signal: None
        trader, _, alerts = _trader(adapter)
        # Only the halt's write fails, so the flatten reaches its first placement.
        outage = _store_outage(trader, halted_only=True)
        outage["down"] = store_down

        trader.request_flatten("desk asked to close")
        if store_down:
            with pytest.raises(OSError, match=OUTAGE):
                trader._poll_cycle()
            outage["down"] = False
        else:
            trader._poll_cycle()
        trader._poll_cycle()

        assert len(_titled(alerts, "Ambiguous Order Placement")) == 1
        # Queued exits left for the next cycle would reach the unresolved one again.
        assert _titled(alerts, "Ambiguous Restored Order") == []
        assert [order.request.symbol for order in trader._active_orders] == [SYMBOLS[0]]
        assert trader._active_orders[0].cancel_requested is True
        assert trader.halt_reset_readiness().reason != "halt_not_persisted"


def test_a_checkpoint_failure_outside_a_halt_is_unchanged():
    adapter = _resting_adapter()
    trader, _, alerts = _trader(adapter)
    _store_outage(trader)

    with pytest.raises(OSError, match=OUTAGE):
        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )

    assert trader._halted is False
    assert trader._halt_unpersisted is False
    assert alerts == []
    adapter.place_order.assert_not_called()
