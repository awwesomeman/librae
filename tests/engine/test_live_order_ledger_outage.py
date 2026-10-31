"""Order work that hits a state-store outage must catch the ledger up afterwards.

An order that fills while the store is down leaves the active book, so the
snapshot no longer carries it: its ``broker_orders`` row and the published
fill have to be written by whatever checkpoint lands next.
"""

from __future__ import annotations

import pytest
from librae.core.strategy import OrderIntent

from tests.engine.live_factories import TEST_CLOCK_NOW
from tests.engine.test_live_flatten import _bars, _trader
from tests.engine.test_live_halt_durability import (
    OUTAGE,
    _buy_two,
    _resting_adapter,
    _store_outage,
)
from tests.engine.test_live_runner import _broker_report


class _PositionSink:
    """A sink that opts into retried delivery and can fail on demand."""

    durable_position_event_delivery = True

    def __init__(self) -> None:
        self.accepted: list[tuple[str, int]] = []
        self.fail = False

    def __call__(self, event, sequence: int) -> None:
        if self.fail:
            raise OSError(OUTAGE)
        self.accepted.append((event.symbol, sequence))


def _fill_as_the_store_drops(adapter, outage) -> None:
    """The broker reports AAA filled, and the store goes down right after."""

    def get_order(order_id, _symbol):
        outage["down"] = True
        return _broker_report(order_id=order_id, quantity=1.0)

    adapter.get_order.side_effect = get_order


def _ledger(trader, tracked) -> object:
    return trader._state_store.orders[tracked.request.client_order_id]


class TestOrderLedgerAfterOutage:
    def test_a_fill_during_a_halt_cancellation_reaches_the_ledger_and_results(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, _, _ = _trader(adapter, on_position_event=sink)
        _buy_two(trader)
        aaa, bbb = trader._active_orders
        outage = _store_outage(trader)
        outage["down"] = False
        _fill_as_the_store_drops(adapter, outage)

        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")

        assert trader._positions["AAA"].quantity == pytest.approx(2.0)
        assert _ledger(trader, aaa).status != "filled"
        assert sink.accepted == [], "a fill is published only once a checkpoint holds it"

        outage["down"] = False
        trader._poll_cycle()

        assert _ledger(trader, aaa).status == "filled"
        assert _ledger(trader, aaa).filled_quantity == pytest.approx(1.0)
        assert _ledger(trader, bbb).status == "cancelled"
        assert trader._state_store.load(trader._state_key).active_orders == []
        assert sink.accepted == [("AAA", 1)]
        adapter.place_order.assert_called_once()

    def test_a_fill_with_no_later_order_work_is_written_next_cycle(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, _, _ = _trader(adapter, on_position_event=sink)
        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )
        [aaa] = trader._active_orders
        outage = _store_outage(trader)
        outage["down"] = False
        _fill_as_the_store_drops(adapter, outage)

        with pytest.raises(OSError, match=OUTAGE):
            trader._advance_live_orders()
        assert trader._active_orders == []

        with pytest.raises(OSError, match=OUTAGE):
            trader._poll_cycle()
        assert sink.accepted == []

        outage["down"] = False
        trader._poll_cycle()

        assert _ledger(trader, aaa).status == "filled"
        assert sink.accepted == [("AAA", 1)]
        assert trader._unpersisted_orders == {}

    def test_a_failed_position_event_write_is_retried_in_order(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, _, _ = _trader(adapter, on_position_event=sink)
        _buy_two(trader)
        adapter.get_order.side_effect = lambda order_id, _s: _broker_report(
            order_id=order_id, quantity=1.0
        )
        sink.fail = True

        trader._advance_live_orders()
        trader._advance_live_orders()

        assert trader._active_orders == []
        assert sink.accepted == []
        assert len(trader._pending_position_events) == 2

        sink.fail = False
        trader._poll_cycle()

        assert sink.accepted == [("AAA", 1), ("BBB", 2)]
        assert trader._pending_position_events == []

    def test_a_sink_that_does_not_opt_in_is_called_once_and_raises(self):
        adapter = _resting_adapter()
        calls: list[int] = []

        def sink(_event, sequence):
            calls.append(sequence)
            raise RuntimeError("user callback exploded")

        trader, _, _ = _trader(adapter, on_position_event=sink)
        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )
        adapter.get_order.side_effect = lambda order_id, _s: _broker_report(
            order_id=order_id, quantity=1.0
        )

        with pytest.raises(RuntimeError, match="exploded"):
            trader._advance_live_orders()
        trader._poll_cycle()

        assert calls == [1]
        assert trader._pending_position_events == []

    def test_a_queued_fill_does_not_make_a_later_halt_look_unpersisted(self):
        adapter = _resting_adapter()
        resting = adapter.get_order.side_effect

        def sink(_event, _sequence):
            raise RuntimeError("user callback exploded")

        trader, _, alerts = _trader(adapter, on_position_event=sink)
        _buy_two(trader)
        aaa, bbb = trader._active_orders
        outage = _store_outage(trader)
        outage["down"] = False
        _fill_as_the_store_drops(adapter, outage)
        with pytest.raises(OSError, match=OUTAGE):
            trader._advance_live_orders()
        outage["down"] = False
        adapter.get_order.side_effect = resting

        trader.halt("desk asked to stop")

        assert trader._halt_unpersisted is False
        assert trader._state_store.load(trader._state_key).halted is True
        assert _ledger(trader, aaa).status == "filled"
        assert _ledger(trader, bbb).status == "cancelled"
        assert not any("not persisted" in alert["message"] for alert in alerts)
