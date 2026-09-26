"""A halt whose checkpoint cannot be written must still reach the operator.

The run stays halted in memory, retries the checkpoint first thing in every
later cycle, and refuses ``reset_halt()`` until the store has recorded the
halt, so nobody resumes from a state the store never saw.
"""

from __future__ import annotations

import pytest
from librae.core.strategy import OrderIntent

from tests.engine.test_live_flatten import SYMBOLS, _adapter, _bars, _titled, _trader
from tests.engine.test_live_operator_halt import _resting
from tests.engine.test_live_runner import TEST_CLOCK_NOW

OUTAGE = "state store unavailable"


def _store_outage(trader, calls: list[str] | None = None) -> dict[str, bool]:
    """Fail every save of a halted checkpoint while ``outage['down']`` is set."""
    store = trader._state_store
    save = store.save
    outage = {"down": True}

    def guarded_save(state, orders=()):
        if calls is not None:
            calls.append("save")
        if outage["down"] and state.halted:
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
        trader._execute_live_decision(
            [
                OrderIntent(action="long", symbol="AAA", quantity=1.0),
                OrderIntent(action="long", symbol="BBB", quantity=1.0),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )
        assert len(trader._active_orders) == 2
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

    def test_flatten_halt_alerts_the_outcome_and_keeps_its_exits(self):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        outage = _store_outage(trader)

        trader.request_flatten("desk asked to close")
        with pytest.raises(OSError, match=OUTAGE):
            trader._poll_cycle()

        assert trader._halted is True
        exits = list(trader._active_orders)
        assert len(exits) == len(SYMBOLS)
        [flatten] = _titled(alerts, "Operator Flatten")
        assert "flattening in progress" in flatten["message"]
        assert "not persisted" in flatten["message"]
        assert OUTAGE in flatten["message"]
        assert trader.halt_reset_readiness().reason == "halt_not_persisted"

        outage["down"] = False
        trader._poll_cycle()

        adapter.cancel_order.assert_not_called()
        assert trader._active_orders == exits
        assert not any(order.cancel_requested for order in exits)
        assert len(_titled(alerts, "Halt Persisted")) == 1

    def test_a_checkpoint_failure_outside_a_halt_is_unchanged(self):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)

        def failing_save(_state, _orders=()):
            raise OSError(OUTAGE)

        trader._state_store.save = failing_save

        with pytest.raises(OSError, match=OUTAGE):
            trader._execute_live_decision(
                [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
            )

        assert trader._halted is False
        assert trader._halt_unpersisted is False
        assert alerts == []
        adapter.place_order.assert_not_called()
