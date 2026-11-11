"""An order that stays tracked without ending is named in an alert, and a cancel is reported as it went.

Order Stuck is a lasting problem: it alerts once, then on the repeat
schedule, and resolves when the order ends. It covers an engine cancel the
venue still has not confirmed a pass after the one that first found it so, a
cancel the venue started on its own that outlasts the order's timeout, and an
order still working a bar after placement with no timeout. A timeout's cancel
the venue only acknowledges is finished by a later poll, as after a restart,
instead of halting at once. A cancel call that raised is never reported as
sent. A leg whose failure meets a store outage still fails its group or
rebalance, and alerts, before the error fails the cycle, and the rebalance
stop holds the bar the leg actually went out on.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from datetime import timedelta

import pytest
from librae.core.strategy import OrderIntent, PortfolioWeights
from librae.live.engine import ReportNotAppliedError
from librae.live.state import MemoryLiveStateStore, StateStoreUnavailableError

from tests.engine.live_factories import TEST_CLOCK_NOW
from tests.engine.test_halt_runtime_events import (
    _continue_on_the_next_bar,
    _Crash,
    _held,
    _next_bar,
    _of_type,
    _refusing_aaa,
    _SameTargets,
    _timeout_trader,
)
from tests.engine.test_live_flatten import _adapter, _bars, _position, _submitted, _trader
from tests.engine.test_live_halt_durability import _resting_adapter
from tests.engine.test_live_operator_halt import _resting
from tests.engine.test_live_refused_rebalance_leg import (
    OUTAGE,
    _hold_pair,
    _outage_from,
    _paired,
)
from tests.engine.test_live_runner import _broker_report

STUCK = "Order Stuck"
REFUSED = "contract not found"


def _exactly(alerts, title: str) -> list[dict]:
    return [alert for alert in alerts if alert["title"].endswith(f"] {title}")]


def _next_poll(trader) -> None:
    """The order work of the next poll cycle, which starts a new pass over the tracked orders."""
    trader._order_pass += 1
    trader._advance_live_orders()


def _unanswered(*_args):
    raise ConnectionError("venue did not answer")


def _buy_aaa(trader):
    trader._execute_live_decision(
        [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
    )
    [aaa] = trader._active_orders
    return aaa


def _clocked_trader(adapter):
    """A trader whose orders time out 30 seconds after placement, its alerts and its clock."""
    now = [TEST_CLOCK_NOW]
    trader, _, alerts = _timeout_trader(
        adapter, MemoryLiveStateStore(restart_durable_for_tests=True), now
    )
    return trader, alerts, now


def _restarted_mid_cancel(adapter, *, grouped: bool | None = None):
    """A trader restarted after its timeout's cancel was recorded, its order still tracked.

    ``grouped`` None buys AAA ungrouped; otherwise a target rebalance closing
    AAA, then BBB, has its AAA leg resting, in group ``pair`` when True. The
    restart's own cancel gets whatever ``adapter`` answers then.
    """
    store = MemoryLiveStateStore(restart_durable_for_tests=True)
    now = [TEST_CLOCK_NOW]
    trader, _, _ = _timeout_trader(adapter, store, now)
    if grouped is None:
        _buy_aaa(trader)
    else:
        trader._positions = {
            symbol: replace(_position(symbol), group_id="pair" if grouped else None)
            for symbol in ("AAA", "BBB")
        }
        trader._execute_live_decision(PortfolioWeights({"BBB": 0.0}), _bars(), TEST_CLOCK_NOW)
        adapter.get_position.side_effect = lambda request: _held(request, "AAA", "BBB")
    now[0] += timedelta(seconds=30)
    answer = adapter.cancel_order.side_effect
    adapter.cancel_order.side_effect = _Crash
    with pytest.raises(_Crash):
        trader._advance_live_orders()
    adapter.cancel_order.side_effect = answer
    trader, events, alerts = _timeout_trader(adapter, store, now)
    trader._initialize_run()
    [aaa] = trader._active_orders
    assert aaa.cancel_requested
    return trader, events, alerts, now, aaa


class TestACancelTheVenueDoesNotConfirm:
    def test_one_the_venue_answers_neither_way_alerts_naming_it_then_on_the_repeat_schedule(self):
        adapter = _resting_adapter()
        adapter.get_order.side_effect = _unanswered
        adapter.cancel_order.side_effect = _unanswered
        trader, _, alerts, now, aaa = _restarted_mid_cancel(adapter)

        for _ in range(4):
            trader._poll_cycle()

        [alert] = _exactly(alerts, STUCK)
        assert f"AAA order_id={aaa.order_id}" in alert["message"]
        assert "has not confirmed the engine's cancel" in alert["message"]
        assert "status unavailable (ConnectionError: venue did not answer)" in alert["message"]
        assert "cancel failed (ConnectionError: venue did not answer)" in alert["message"]
        assert trader._active_orders == [aaa]
        assert trader._halted is False

        now[0] += timedelta(hours=1)
        trader._poll_cycle()

        assert len(_exactly(alerts, STUCK)) == 2
        assert "still so after 1h" in _exactly(alerts, STUCK)[-1]["message"]

    def test_one_a_halt_owes_alerts_too(self):
        adapter = _resting_adapter()
        trader, alerts, _ = _clocked_trader(adapter)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = _unanswered
        adapter.cancel_order.side_effect = _unanswered

        trader.halt("desk asked to stop")
        trader._poll_cycle()
        trader._poll_cycle()

        [alert] = _exactly(alerts, STUCK)
        assert f"AAA order_id={aaa.order_id}" in alert["message"]

    def test_it_resolves_once_the_order_ends(self):
        adapter = _resting_adapter()
        adapter.get_order.side_effect = _unanswered
        adapter.cancel_order.side_effect = _unanswered
        trader, _, alerts, _, _ = _restarted_mid_cancel(adapter)
        trader._poll_cycle()
        trader._poll_cycle()
        assert len(_exactly(alerts, STUCK)) == 1

        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
        trader._poll_cycle()

        assert trader._active_orders == []
        [resolved] = _exactly(alerts, "Resolved")
        assert resolved["message"].startswith(f"{STUCK} cleared after")


class TestATimeoutCancelTheVenueOnlyAcknowledges:
    """IB answers a cancel PendingCancel before it confirms it (unverified against IB)."""

    def _timed_out_leg(self):
        adapter = _resting_adapter()
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        now = [TEST_CLOCK_NOW]
        trader, events, alerts = _timeout_trader(adapter, store, now)
        trader._positions = {
            symbol: replace(_position(symbol), group_id="pair") for symbol in ("AAA", "BBB")
        }
        trader._execute_live_decision(PortfolioWeights({"BBB": 0.0}), _bars(), TEST_CLOCK_NOW)
        [aaa] = trader._active_orders
        now[0] += timedelta(seconds=30)
        _next_poll(trader)
        return adapter, trader, events, alerts, aaa

    def test_is_finished_by_a_later_poll_as_order_timeout(self):
        adapter, trader, events, alerts, aaa = self._timed_out_leg()

        assert trader._halted is False
        assert trader._active_orders == [aaa]
        assert (aaa.status, aaa.cancel_requested) == ("cancel_pending", True)
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
        _next_poll(trader)
        trader._continue_live_rebalance(_bars(), TEST_CLOCK_NOW + timedelta(hours=1))

        assert trader._halted is False
        assert _submitted(adapter) == ["AAA"]
        adapter.cancel_order.assert_called_once()
        assert trader._live_rebalance is None
        [timeout] = _exactly(alerts, "Order Timeout")
        assert "target rebalance stopped" in timeout["message"]
        assert len(_of_type(events, "group_failed")) == 1
        assert _exactly(alerts, STUCK) == []

    def test_alerts_once_still_unconfirmed_a_poll_later_without_resending(self):
        adapter, trader, _, alerts, aaa = self._timed_out_leg()
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")

        _next_poll(trader)
        _next_poll(trader)
        _next_poll(trader)

        assert trader._halted is False
        adapter.cancel_order.assert_called_once()
        [alert] = _exactly(alerts, STUCK)
        assert f"AAA order_id={aaa.order_id} (cancel_pending)" in alert["message"]
        assert "cancel already pending" in alert["message"]


class TestAnOrderWorkingPastItsBound:
    @pytest.mark.parametrize(
        ("timeout", "bound"),
        [(30, timedelta(seconds=30)), (None, timedelta(hours=1))],
        ids=["past-its-timeout", "no-timeout-past-a-bar"],
    )
    def test_a_cancel_the_venue_started_alerts_and_is_waited_for(self, timeout, bound):
        adapter = _resting_adapter()
        trader, alerts, now = _clocked_trader(adapter)
        trader._live_order_timeout_seconds = timeout
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        now[0] += bound - timedelta(seconds=1)
        trader._poll_cycle()
        assert _exactly(alerts, STUCK) == []

        now[0] += timedelta(seconds=1)
        trader._poll_cycle()
        trader._poll_cycle()

        [alert] = _exactly(alerts, STUCK)
        assert f"AAA order_id={aaa.order_id} (cancel_pending)" in alert["message"]
        assert "the venue has not finished a cancel it started" in alert["message"]
        adapter.cancel_order.assert_not_called()
        assert trader._halted is False
        assert trader._active_orders == [aaa]

        now[0] += timedelta(hours=1)
        trader._poll_cycle()
        assert len(_exactly(alerts, STUCK)) == 2

    def test_one_working_a_bar_with_no_timeout_alerts_naming_it(self):
        adapter = _resting_adapter()
        trader, alerts, now = _clocked_trader(adapter)
        trader._live_order_timeout_seconds = None
        aaa = _buy_aaa(trader)
        now[0] += timedelta(minutes=59)
        trader._poll_cycle()
        assert _exactly(alerts, STUCK) == []

        now[0] += timedelta(minutes=1)
        for _ in range(3):
            trader._poll_cycle()

        [alert] = _exactly(alerts, STUCK)
        assert f"AAA order_id={aaa.order_id} (accepted)" in alert["message"]
        assert "no live_order_timeout_seconds" in alert["message"]
        assert "no strategy decision runs while it is tracked" in alert["message"]
        adapter.cancel_order.assert_not_called()

        adapter.get_order.side_effect = lambda order_id, _s: _broker_report(order_id=order_id)
        trader._poll_cycle()

        assert trader._active_orders == []
        [resolved] = _exactly(alerts, "Resolved")
        assert resolved["message"].startswith(f"{STUCK} cleared after")

    def test_one_under_its_timeout_does_not_alert(self):
        adapter = _resting_adapter()
        trader, alerts, now = _clocked_trader(adapter)
        _buy_aaa(trader)
        now[0] += timedelta(seconds=29)

        trader._poll_cycle()

        assert alerts == []


class TestACancelCallThatRaisedIsNotReportedSent:
    """An adapter can refuse a cancel before anything reaches the venue."""

    def test_after_a_status_read_that_applied(self):
        adapter = _resting_adapter()
        trader, alerts, _ = _clocked_trader(adapter)
        aaa = _buy_aaa(trader)
        adapter.cancel_order.side_effect = ValueError(REFUSED)

        with pytest.raises(ReportNotAppliedError):
            trader.halt("desk asked to stop")

        [halt] = _exactly(alerts, "Manual Halt")
        assert f"AAA order_id={aaa.order_id} broker report not applied" in halt["message"]
        assert f"cancel not confirmed (ValueError: {REFUSED})" in halt["message"]
        assert "cancel sent" not in halt["message"]

    def test_in_a_halts_pass_in_the_same_cycle(self):
        adapter = _resting_adapter()
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        trader, _, alerts, _, aaa = _restarted_mid_cancel(adapter)
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")
        adapter.cancel_order.side_effect = ValueError(REFUSED)

        with pytest.raises(ReportNotAppliedError):
            _next_poll(trader)

        [halt] = _exactly(alerts, "Order Reconciliation Failed")
        assert trader._halted is True
        assert halt["message"].count(f"cancel not confirmed (ValueError: {REFUSED})") == 2
        assert "cancel already sent" not in halt["message"]
        assert trader._active_orders == [aaa]


class TestACancelAnswerRecordedInAStoreOutage:
    """Its order still fails its group or rebalance, and alerts, before the error fails the cycle."""

    @pytest.mark.parametrize("grouped", [True, False])
    @pytest.mark.parametrize("ends", ["after_a_restart", "in_the_timeout_cycle"])
    def test_a_timeout_cancel_still_fails_its_leg(self, ends, grouped):
        adapter = _resting_adapter()
        if ends == "after_a_restart":
            adapter.cancel_order.side_effect = lambda order_id, _s: _resting(
                order_id, "pending_cancel"
            )
            trader, events, alerts, now, _ = _restarted_mid_cancel(adapter, grouped=grouped)
            adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
        else:
            store = MemoryLiveStateStore(restart_durable_for_tests=True)
            now = [TEST_CLOCK_NOW]
            trader, events, alerts = _timeout_trader(adapter, store, now)
            trader._positions = {
                symbol: replace(_position(symbol), group_id="pair" if grouped else None)
                for symbol in ("AAA", "BBB")
            }
            trader._execute_live_decision(PortfolioWeights({"BBB": 0.0}), _bars(), TEST_CLOCK_NOW)
            now[0] += timedelta(seconds=30)
        outage = _outage_from(
            trader, lambda _state, orders: any(row.status == "cancelled" for row in orders)
        )

        with pytest.raises(StateStoreUnavailableError):
            _next_poll(trader)

        [alert] = _exactly(alerts, "Order Timeout")
        assert "AAA" in alert["message"]
        assert OUTAGE in alert["message"]
        assert _exactly(alerts, "Order Timeout Cancellation Failed") == []
        outage["down"] = False
        trader._persist_state()  # as the next poll does first
        trader._offer_held_runtime_events()
        trader._continue_live_rebalance(_bars(), now[0] + timedelta(hours=1))

        assert _submitted(adapter) == ["AAA"]
        assert trader._live_rebalance is None
        assert trader._halted is (not grouped)
        if grouped:
            assert "target rebalance stopped" in alert["message"]
            assert len(_of_type(events, "group_failed")) == 1
        else:
            assert "halt not persisted" in alert["message"]


def _trio(adapter):
    """A trader whose three-leg entry group's first leg, AAA, the venue refuses."""
    adapter.place_order.side_effect = _refusing_aaa("rejected")
    trader, events, alerts = _trader(adapter)
    trader._positions = {}
    legs = [
        OrderIntent(action="long", symbol=symbol, quantity=1.0, group_id="trio")
        for symbol in ("AAA", "BBB", "DUST")
    ]
    return trader, events, alerts, legs


def _cancelled_rows(store) -> list[str]:
    return sorted(row.request.symbol for row in store.orders.values() if row.status == "cancelled")


class TestAGroupRefusedInAStoreOutage:
    """Its unsent legs leave the queue in the write that records the refusal."""

    def test_an_outage_at_the_refusal_drops_every_unsent_leg_and_alerts(self):
        """A third leg stayed queued and went out alone, unhedged, once the store came back."""
        adapter = _adapter(below_minimum="")
        trader, events, alerts, legs = _trio(adapter)
        outage = _outage_from(
            trader, lambda _state, orders: any(row.status == "rejected" for row in orders)
        )

        with pytest.raises(StateStoreUnavailableError):
            trader._execute_live_decision(legs, _bars(), TEST_CLOCK_NOW)

        [alert] = _exactly(alerts, "Order Rejected")
        assert "group 'trio' cancelled" in alert["message"]
        assert OUTAGE in alert["message"]
        assert trader._active_orders == []
        outage["down"] = False
        trader._persist_state()
        trader._offer_held_runtime_events()
        _next_poll(trader)

        assert _submitted(adapter) == ["AAA"]
        assert len(_of_type(events, "group_failed")) == 1
        assert _cancelled_rows(trader._state_store) == ["BBB", "DUST"]
        assert trader._halted is False

    def test_a_restart_once_the_refusal_is_written_does_not_send_the_others(self):
        """The drop was a later write, so a restart restored the other legs queued."""
        adapter = _adapter(below_minimum="")
        trader, _, _, legs = _trio(adapter)
        store = trader._state_store
        save = store.save
        written = {"refusal": False}

        def save_until_the_refusal_is_written(state, orders=()):
            if written["refusal"]:
                raise StateStoreUnavailableError(OUTAGE)
            save(state, orders)
            written["refusal"] = any(row.status == "rejected" for row in orders)

        store.save = save_until_the_refusal_is_written
        with suppress(StateStoreUnavailableError):
            trader._execute_live_decision(legs, _bars(), TEST_CLOCK_NOW)
        store.save = save

        restored, _, _ = _trader(adapter, state_store=store)
        restored._positions = {}
        restored._initialize_run()
        _next_poll(restored)

        assert restored._active_orders == []
        assert _submitted(adapter) == ["AAA"]
        assert _cancelled_rows(store) == ["BBB", "DUST"]


class TestARebalanceStopHoldsTheBarItsLegWentOutOn:
    def test_the_bar_that_poll_decides_does_not_send_it_again(self):
        """A leg a failed write left queued went out at the next poll, then that poll's bar resent it."""
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa("rejected")
        trader, strategy, events, _, bar_times = _paired(adapter)
        outage = _outage_from(
            trader, lambda _state, orders: any(not row.placement_attempted for row in orders)
        )
        with pytest.raises(StateStoreUnavailableError):
            _next_bar(trader, bar_times)
        outage["down"] = False

        _next_poll(trader)
        _next_bar(trader, bar_times)

        assert _submitted(adapter) == ["AAA"]
        assert strategy.calls == 0
        assert len(_of_type(events, "group_failed")) == 1
        stored = trader._state_store.load(trader._state_key)
        assert stored.rebalance_stopped_at == bar_times[-1]

        _next_bar(trader, bar_times)

        assert strategy.calls == 1, "the next bar's decision replans it once"
        assert _submitted(adapter) == ["AAA", "AAA"]

    @pytest.mark.parametrize("ends", ["canceled", "rejected", "timeout"])
    def test_a_leg_sent_on_its_bar_that_ends_before_the_next_lets_that_one_decide(self, ends):
        """Only a leg the order poll sent ahead of a bar holds that bar too."""
        adapter = _resting_adapter()
        now = [TEST_CLOCK_NOW]
        strategy = _SameTargets({"BBB": 0.0})
        trader, _, _ = _trader(adapter, strategy=strategy, clock=lambda: now[0])
        trader._live_order_timeout_seconds = 30
        bar_times = _continue_on_the_next_bar(trader, strategy.weights)
        _hold_pair(trader)
        _next_bar(trader, bar_times)
        assert [order.request.symbol for order in trader._active_orders] == ["AAA"]
        if ends == "timeout":
            now[0] += timedelta(seconds=30)
            adapter.cancel_order.side_effect = lambda order_id, _s: _resting(
                order_id, "pending_cancel"
            )
            _next_poll(trader)
            ends = "canceled"
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, ends)
        _next_poll(trader)
        assert trader._live_rebalance is None
        assert trader._halted is False

        _next_bar(trader, bar_times)

        assert strategy.calls == 1
        assert _submitted(adapter) == ["AAA", "AAA"]
