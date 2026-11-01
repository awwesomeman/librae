"""Order work that hits a state-store outage must catch the ledger up afterwards.

An order that fills while the store is down leaves the active book, so the
snapshot no longer carries it: its ``broker_orders`` row and the published
fill have to be written by whatever checkpoint lands next.
"""

from __future__ import annotations

import pytest
from librae.core.strategy import OrderIntent

from tests.engine.live_factories import TEST_CLOCK_NOW
from tests.engine.test_live_flatten import _bars, _titled, _trader
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
        self.refused: set[int] = set()

    def __call__(self, event, sequence: int) -> None:
        if self.fail or sequence in self.refused:
            raise OSError(OUTAGE)
        self.accepted.append((event.symbol, sequence))


def _fill_as_the_store_drops(adapter, outage) -> None:
    """The broker reports AAA filled, and the store goes down right after."""

    def get_order(order_id, _symbol):
        outage["down"] = True
        return _broker_report(order_id=order_id, quantity=1.0)

    adapter.get_order.side_effect = get_order


def _fill_both(trader, adapter) -> None:
    """AAA and BBB fill one after the other, one event each."""
    _buy_two(trader)
    adapter.get_order.side_effect = lambda order_id, _s: _broker_report(
        order_id=order_id, quantity=1.0
    )
    trader._advance_live_orders()
    trader._advance_live_orders()
    assert trader._active_orders == []


def _dropped(events) -> list[int]:
    return [
        event.detail["sequence"]
        for event in events
        if event.event_type == "decision_skipped"
        and event.detail.get("reason") == "position_event_delivery_failed"
    ]


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


class TestPositionEventBacklog:
    def test_a_full_backlog_drops_the_oldest_loudly_then_clears(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, events, alerts = _trader(adapter, on_position_event=sink)
        trader.POSITION_EVENT_RECOVERY_CYCLES = 1
        trader.MAX_PENDING_POSITION_EVENTS = 1
        sink.fail = True

        _fill_both(trader, adapter)

        assert _dropped(events) == [1]
        assert [sequence for _, sequence in trader._pending_position_events] == [2]
        assert trader.position_event_delivery_degraded is True
        assert len(_titled(alerts, "Position Event Delivery Failing")) == 1

        sink.fail = False
        trader._poll_cycle()

        assert sink.accepted == [("BBB", 2)]
        assert trader.position_event_delivery_degraded is False

    def test_an_event_the_sink_never_accepts_stops_blocking_later_ones(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, events, _ = _trader(adapter, on_position_event=sink)
        trader.MAX_PENDING_POSITION_EVENTS = 1
        sink.refused = {1}

        _fill_both(trader, adapter)

        assert sink.accepted == [("BBB", 2)]
        assert _dropped(events) == [1]
        assert trader._pending_position_events == []

    def test_an_event_the_sink_keeps_refusing_alerts_at_once_and_is_dropped(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, events, alerts = _trader(adapter, on_position_event=sink)
        trader.POSITION_EVENT_RECOVERY_CYCLES = 1
        trader.MAX_POSITION_EVENT_ATTEMPTS = 3
        sink.refused = {1}

        _fill_both(trader, adapter)
        trader._poll_cycle()

        assert trader.position_event_delivery_degraded is True
        assert len(_titled(alerts, "Position Event Delivery Failing")) == 1
        assert [sequence for _, sequence in trader._pending_position_events] == [1, 2]
        assert _dropped(events) == []

        trader._poll_cycle()
        assert _dropped(events) == [], "dropped before its third refused cycle"
        trader._poll_cycle()
        assert _dropped(events) == [1]
        trader._poll_cycle()

        assert sink.accepted == [("BBB", 2)]
        assert trader._pending_position_events == []
        assert trader.position_event_delivery_degraded is False
        assert len(_titled(alerts, "Position Event Delivery Failing")) == 1

    def test_a_drop_the_runtime_sink_cannot_record_stays_queryable(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, _, _ = _trader(adapter, on_position_event=sink)
        trader.MAX_POSITION_EVENT_ATTEMPTS = 1
        sink.refused = {1}
        _fill_both(trader, adapter)

        def failing_record(_event):
            raise RuntimeError("runtime event sink down")

        trader._on_runtime_event = failing_record
        trader._poll_cycle()
        trader._poll_cycle()

        assert trader.dropped_position_event_sequences == (1,)
        assert sink.accepted == [("BBB", 2)]

    def test_a_full_queue_with_a_failing_runtime_sink_still_queues_every_event(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, _, _ = _trader(adapter, on_position_event=sink)
        sink.fail = True
        _fill_both(trader, adapter)
        (aaa, _), (bbb, _) = trader._pending_position_events
        trader.MAX_PENDING_POSITION_EVENTS = 2

        def failing_record(_event):
            raise RuntimeError("runtime event sink down")

        trader._on_runtime_event = failing_record
        for sequence, event in ((3, aaa), (4, bbb)):
            trader._queue_position_event(event, sequence)

        assert [sequence for _, sequence in trader._pending_position_events] == [3, 4]
        assert trader.dropped_position_event_sequences == (1, 2)

    def test_a_flapping_sink_alerts_once_until_it_stays_healthy(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, _, alerts = _trader(adapter, on_position_event=sink)
        trader.POSITION_EVENT_RECOVERY_CYCLES = 3
        sink.fail = True
        _fill_both(trader, adapter)
        [(event, _), _] = trader._pending_position_events

        for sequence in (3, 4, 5):
            sink.fail = False
            trader._poll_cycle()
            sink.fail = True
            trader._queue_position_event(event, sequence)
            trader._flush_pending_position_events()

        assert trader.position_event_delivery_degraded is True
        assert len(_titled(alerts, "Position Event Delivery Failing")) == 1

        sink.fail = False
        for _ in range(3):
            assert trader.position_event_delivery_degraded is True
            trader._poll_cycle()
        assert trader.position_event_delivery_degraded is False

        sink.fail = True
        trader._queue_position_event(event, 6)
        trader._flush_pending_position_events()
        assert len(_titled(alerts, "Position Event Delivery Failing")) == 2

    def test_a_sink_that_recovers_within_the_attempts_loses_nothing(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, events, alerts = _trader(adapter, on_position_event=sink)
        trader.POSITION_EVENT_RECOVERY_CYCLES = 1
        trader.MAX_POSITION_EVENT_ATTEMPTS = 3
        sink.fail = True

        _fill_both(trader, adapter)
        for _ in range(2):
            trader._poll_cycle()
        sink.fail = False
        trader._poll_cycle()

        assert sink.accepted == [("AAA", 1), ("BBB", 2)]
        assert _dropped(events) == []
        assert trader.position_event_delivery_degraded is False
        assert len(_titled(alerts, "Position Event Delivery Failing")) == 1

    def test_a_backlog_under_the_bound_drops_nothing(self):
        adapter = _resting_adapter()
        sink = _PositionSink()
        trader, events, alerts = _trader(adapter, on_position_event=sink)
        sink.fail = True

        _fill_both(trader, adapter)

        assert _dropped(events) == []
        assert len(trader._pending_position_events) == 2
        # The first refusal already reports itself, before anything is dropped.
        assert trader.position_event_delivery_degraded is True
        assert len(_titled(alerts, "Position Event Delivery Failing")) == 1


REJECTED = "numeric field overflow"


def _reject_row_of(trader, tracked, calls: list[tuple[str, ...]] | None = None) -> None:
    """Refuse every save carrying ``tracked``'s row once it has filled, as a
    constraint or overflow does, while saves without it still land."""
    store = trader._state_store
    save = store.save
    rejected_id = tracked.request.client_order_id

    def refusing_save(state, orders=()):
        ids = tuple(order.request.client_order_id for order in orders)
        if calls is not None:
            calls.append(ids)
        if any(
            order.request.client_order_id == rejected_id and order.status == "filled"
            for order in orders
        ):
            raise ValueError(REJECTED)
        save(state, orders)

    store.save = refusing_save


def _rejected(events) -> list[str]:
    return [
        event.detail["client_order_id"]
        for event in events
        if event.event_type == "decision_skipped"
        and event.detail.get("reason") == "order_row_rejected"
    ]


def _fill_first(trader, adapter) -> None:
    """The broker reports the head order filled; its checkpoint carries the row."""
    adapter.get_order.side_effect = lambda order_id, _s: _broker_report(
        order_id=order_id, quantity=1.0
    )
    trader._advance_live_orders()


class TestRejectedOrderRow:
    def test_a_rejected_row_does_not_block_the_halt_or_its_cancellation(self):
        adapter = _resting_adapter()
        resting = adapter.get_order.side_effect
        trader, events, alerts = _trader(adapter)
        _buy_two(trader)
        aaa, bbb = trader._active_orders
        _reject_row_of(trader, aaa)
        _fill_first(trader, adapter)
        adapter.get_order.side_effect = resting

        trader.halt("desk asked to stop")
        for _ in range(3):
            trader._poll_cycle()

        assert trader._halt_unpersisted is False
        assert trader._halt_cancels_pending is False
        assert trader._state_store.load(trader._state_key).halted is True
        assert trader._active_orders == []
        assert _ledger(trader, bbb).status == "cancelled"
        assert _rejected(events) == [aaa.request.client_order_id]
        assert trader.order_ledger_degraded is True
        assert len(_titled(alerts, "Order Row Rejected")) == 1
        assert not any("not persisted" in alert["message"] for alert in alerts)

    def test_the_snapshot_lands_and_the_other_rows_are_written(self):
        adapter = _resting_adapter()
        trader, events, alerts = _trader(adapter)
        _buy_two(trader)
        aaa, bbb = trader._active_orders
        calls: list[tuple[str, ...]] = []
        _reject_row_of(trader, aaa, calls)
        aaa_id, bbb_id = aaa.request.client_order_id, bbb.request.client_order_id
        # BBB's row rides with AAA's, as rows a failed write left behind do;
        # the ledger holds it again only if its own write lands.
        trader._unpersisted_orders[bbb_id] = bbb
        del trader._state_store.orders[bbb_id]

        _fill_first(trader, adapter)

        # Together, snapshot alone, each row alone (a refused one twice), and
        # the snapshot again to tell a refused row from a store that went down.
        assert calls[:6] == [(bbb_id, aaa_id), (), (bbb_id,), (aaa_id,), (aaa_id,), ()]
        assert bbb_id in trader._state_store.orders
        assert trader._unpersisted_orders == {}
        assert trader._state_store.load(trader._state_key).active_orders == [bbb]
        assert _rejected(events) == [aaa_id]
        assert len(_titled(alerts, "Order Row Rejected")) == 1

    def test_an_outage_still_keeps_every_row_and_raises(self):
        adapter = _resting_adapter()
        trader, events, alerts = _trader(adapter)
        _buy_two(trader)
        [aaa, _] = trader._active_orders
        outage = _store_outage(trader)

        with pytest.raises(OSError, match=OUTAGE):
            _fill_first(trader, adapter)

        assert list(trader._unpersisted_orders) == [aaa.request.client_order_id]
        assert _rejected(events) == []
        assert trader.order_ledger_degraded is False
        assert _titled(alerts, "Order Row Rejected") == []
        outage["down"] = False
        trader._poll_cycle()
        assert _ledger(trader, aaa).status == "filled"

    def test_a_store_that_drops_partway_keeps_the_rows_and_raises(self):
        adapter = _resting_adapter()
        trader, events, _ = _trader(adapter)
        _buy_two(trader)
        aaa, bbb = trader._active_orders
        store = trader._state_store
        save = store.save
        writes = {"left": 2}

        def failing_save(state, orders=()):
            # The joint write fails, the snapshot alone lands, then the store is gone.
            if orders and writes["left"] == 2:
                raise OSError(OUTAGE)
            if writes["left"] == 0:
                raise OSError(OUTAGE)
            writes["left"] -= 1
            save(state, orders)

        store.save = failing_save
        trader._unpersisted_orders[bbb.request.client_order_id] = bbb

        with pytest.raises(OSError, match=OUTAGE):
            _fill_first(trader, adapter)

        # BBB landed alone before the store went; AAA stays carried.
        assert list(trader._unpersisted_orders) == [aaa.request.client_order_id]
        assert _rejected(events) == []
        assert trader.order_ledger_degraded is False

    def test_a_row_that_fails_once_is_written_not_dropped(self):
        adapter = _resting_adapter()
        trader, events, alerts = _trader(adapter)
        _buy_two(trader)
        aaa, _ = trader._active_orders
        store = trader._state_store
        save = store.save
        aaa_id = aaa.request.client_order_id
        dropped = {"aaa": 2}

        def flaky_save(state, orders=()):
            # The joint write and the first lone write lose their connection.
            carries_aaa = any(o.request.client_order_id == aaa_id for o in orders)
            if carries_aaa and dropped["aaa"]:
                dropped["aaa"] -= 1
                raise OSError("server closed the connection")
            save(state, orders)

        store.save = flaky_save

        _fill_first(trader, adapter)

        assert _ledger(trader, aaa).status == "filled"
        assert _rejected(events) == []
        assert trader.order_ledger_degraded is False
        assert _titled(alerts, "Order Row Rejected") == []

    def test_several_refused_rows_alert_once_even_if_the_event_sink_fails(self):
        adapter = _resting_adapter()
        trader, events, alerts = _trader(adapter)
        _buy_two(trader)
        aaa, bbb = trader._active_orders
        _reject_row_of(trader, aaa)

        def failing_sink(_event):
            raise RuntimeError("event sink down")

        trader._on_runtime_event = failing_sink
        adapter.get_order.side_effect = lambda order_id, _s: _broker_report(
            order_id=order_id, quantity=1.0
        )

        with pytest.raises(RuntimeError, match="event sink down"):
            trader._advance_live_orders()

        assert trader.order_ledger_degraded is True
        assert len(_titled(alerts, "Order Row Rejected")) == 1

        _reject_row_of(trader, bbb)
        trader._on_runtime_event = events.append
        while trader._active_orders:
            trader._advance_live_orders()

        assert _rejected(events) == [bbb.request.client_order_id]
        assert trader._unpersisted_orders == {}
        assert len(_titled(alerts, "Order Row Rejected")) == 1
