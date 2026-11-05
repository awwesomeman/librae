"""An order whose venue state the engine cannot confirm is still cancelled.

A status report that cannot be applied to its order leaves that order working
at the venue as far as the engine knows, so it stays under its timeout, does
not hold up the cycle or an operator flatten, and is named in an alert. A halt
sends a cancel to every tracked order the venue has not confirmed ended,
whether or not the venue reports a cancel pending and whether or not its
status could be read, and stays unfinished until the venue confirms each one.
A cancel the engine sent and the venue reports pending is not sent again.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from librae.core.run_config import RiskPolicy
from librae.core.strategy import OrderIntent
from librae.live.executor import LiveExecutor
from librae.live.state import MemoryLiveStateStore

from tests.engine.live_factories import TEST_CLOCK_NOW
from tests.engine.test_halt_runtime_events import _timeout_trader
from tests.engine.test_live_flatten import _bars, _position, _submitted, _titled
from tests.engine.test_live_halt_durability import (
    OUTAGE,
    _answer_about_another_order,
    _partly_filled,
    _resting_adapter,
    _store_outage,
    _unappliable,
)
from tests.engine.test_live_operator_halt import _resting
from tests.engine.test_live_runner import _broker_report

NOT_APPLIED = "Order Report Not Applied"


def _trader(adapter):
    """A trader whose orders time out 30 seconds after placement, and its clock."""
    now = [TEST_CLOCK_NOW]
    trader, _, alerts = _timeout_trader(
        adapter, MemoryLiveStateStore(restart_durable_for_tests=True), now
    )
    return trader, alerts, now


def _buy_aaa(trader):
    trader._execute_live_decision(
        [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
    )
    [aaa] = trader._active_orders
    return aaa


def _exactly(alerts, title: str) -> list[dict]:
    return [alert for alert in alerts if alert["title"].endswith(f"] {title}")]


class TestAReportThatCannotBeAppliedOnAPoll:
    @pytest.mark.parametrize("mismatch", ["order_id", "filled_backwards"])
    def test_it_alerts_naming_the_order_and_is_cancelled_at_its_timeout(self, mismatch):
        adapter = _resting_adapter()
        adapter.place_order.side_effect = lambda signal: _partly_filled(signal["client_order_id"])
        adapter.cancel_order.side_effect = lambda order_id, _s: _partly_filled(order_id, "canceled")
        trader, alerts, now = _trader(adapter)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda order_id, _s: _unappliable(mismatch, order_id)

        trader._poll_cycle()
        trader._poll_cycle()

        assert trader._halted is False
        adapter.cancel_order.assert_not_called()
        [alert] = _titled(alerts, NOT_APPLIED)
        assert f"AAA order_id={aaa.order_id} broker report not applied" in alert["message"]
        assert trader._active_orders == [aaa]

        now[0] += timedelta(seconds=30)
        trader._poll_cycle()

        adapter.cancel_order.assert_called_once()
        assert adapter.cancel_order.call_args.args[0] == aaa.order_id
        assert trader._active_orders == []
        assert trader._halted is True, "an ungrouped order's timeout halts"
        [timeout] = _exactly(alerts, "Order Timeout")
        assert f"AAA order_id={aaa.order_id}" in timeout["message"]
        assert "cancelling it at its timeout" in _titled(alerts, NOT_APPLIED)[-1]["message"]

    def test_one_first_met_past_its_timeout_is_named_too(self):
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")
        now[0] += timedelta(seconds=30)

        trader._poll_cycle()

        adapter.cancel_order.assert_called_once()
        [alert] = _titled(alerts, NOT_APPLIED)
        assert f"AAA order_id={aaa.order_id} broker report not applied" in alert["message"]
        assert len(_exactly(alerts, "Order Timeout")) == 1

    def test_it_does_not_hold_up_a_requested_flatten(self):
        adapter = _resting_adapter()
        trader, alerts, _ = _trader(adapter)
        trader._positions = {"AAA": _position("AAA")}
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")
        trader.request_flatten("desk asked to close")

        trader._poll_cycle()

        adapter.cancel_order.assert_called_once()
        assert adapter.cancel_order.call_args.args[0] == aaa.order_id
        assert trader._halted is True
        assert len(_exactly(alerts, "Operator Flatten")) == 1
        assert _submitted(adapter) == ["AAA", "AAA"], "the entry, then the flatten's exit"
        [alert] = _titled(alerts, NOT_APPLIED)
        assert f"AAA order_id={aaa.order_id} broker report not applied" in alert["message"]

    def test_a_flatten_waits_for_a_cancel_it_cannot_confirm_without_halting(self):
        """Every answer names another order, so the venue never confirms the cancel.

        Halting would refuse the flatten, so even past the order's timeout it
        resends the cancel each cycle and goes ahead once an answer applies.
        """
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        trader._positions = {"AAA": _position("AAA")}
        aaa = _buy_aaa(trader)
        undo = _answer_about_another_order(adapter)
        trader.request_flatten("desk asked to close")

        for cycle in range(1, 4):
            trader._poll_cycle()
            assert adapter.cancel_order.call_count == cycle, "one cancel per cycle"
        now[0] += timedelta(seconds=30)
        trader._poll_cycle()

        assert adapter.cancel_order.call_count == 4
        assert trader._halted is False
        assert trader._flatten_request is not None, "the flatten still waits"
        [alert] = _titled(alerts, NOT_APPLIED)
        assert f"AAA order_id={aaa.order_id} broker report not applied" in alert["message"]
        assert "the operator flatten waits for it" in alert["message"]

        undo()
        trader._poll_cycle()

        assert trader._halted is True
        assert len(_exactly(alerts, "Operator Flatten")) == 1
        assert _submitted(adapter) == ["AAA", "AAA"], "the entry, then the flatten's exit"

    def test_a_flatten_waiting_on_a_venue_that_does_not_answer_says_so_once(self):
        """No cycle fails and no timeout acts here, so only this alert speaks for it."""
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        trader._positions = {"AAA": _position("AAA")}
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = ConnectionError("venue down")
        adapter.cancel_order.side_effect = ConnectionError("venue down")
        trader.request_flatten("desk asked to close")

        for _ in range(3):
            trader._poll_cycle()
        now[0] += timedelta(seconds=30)
        trader._poll_cycle()

        assert adapter.cancel_order.call_count == 4, "one attempt per cycle"
        assert trader._halted is False
        assert trader._flatten_request is not None
        [waiting] = _exactly(alerts, "Operator Flatten Waiting")
        assert f"AAA order_id={aaa.order_id} (accepted)" in waiting["message"]

    @pytest.mark.parametrize("failure", ["lookup", "quantity"])
    def test_a_status_read_that_fails_does_not_hold_up_a_flatten(self, failure):
        adapter = _resting_adapter()
        trader, alerts, _ = _trader(adapter)
        trader._positions = {"AAA": _position("AAA")}
        aaa = _buy_aaa(trader)
        if failure == "lookup":
            adapter.get_order.side_effect = LookupError(f"IBKR order not found: {aaa.order_id}")
        else:
            adapter.get_order.side_effect = lambda order_id, _s: _unappliable("quantity", order_id)
        trader.request_flatten("desk asked to close")

        trader._poll_cycle()

        adapter.cancel_order.assert_called_once()
        assert trader._halted is True
        assert len(_exactly(alerts, "Operator Flatten")) == 1
        assert _submitted(adapter) == ["AAA", "AAA"], "the entry, then the flatten's exit"

    def test_after_a_halt_it_alerts_naming_the_order_without_resending_a_pending_cancel(self):
        adapter = _resting_adapter()
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        trader, alerts, _ = _trader(adapter)
        aaa = _buy_aaa(trader)
        trader.halt("desk asked to stop")
        adapter.cancel_order.assert_called_once()
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")

        trader._poll_cycle()
        trader._poll_cycle()

        adapter.cancel_order.assert_called_once()
        [alert] = _titled(alerts, NOT_APPLIED)
        assert f"AAA order_id={aaa.order_id} broker report not applied" in alert["message"]
        assert "cancel already pending" in alert["message"]

        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
        trader._poll_cycle()

        assert trader._active_orders == []
        assert len(_titled(alerts, "Halt Cancellation Completed")) == 1

    def test_a_signalled_repeat_halt_answers_naming_the_order_without_failing_the_cycle(self):
        adapter = _resting_adapter()
        trader, alerts, _ = _trader(adapter)
        aaa = _buy_aaa(trader)
        _answer_about_another_order(adapter)
        with pytest.raises(RuntimeError, match="not applied"):
            trader.halt("desk asked to stop")

        trader.request_halt("desk asked again")
        trader._poll_cycle()

        assert adapter.cancel_order.call_count == 2, "one per pass"
        assert trader._halt_cancels_pending is True
        _, repeat = _exactly(alerts, "Manual Halt")
        assert "desk asked again; already halted; cancellation owed" in repeat["message"]
        assert f"AAA order_id={aaa.order_id} broker report not applied" in repeat["message"]
        assert _titled(alerts, NOT_APPLIED) == []

    def test_a_store_failure_after_the_cancel_still_fails_the_halt(self):
        adapter = _resting_adapter()
        trader, alerts, _ = _trader(adapter)
        aaa = _buy_aaa(trader)
        outage = _store_outage(trader)
        outage["down"] = False

        def cancel(order_id, _symbol):
            outage["down"] = True
            return _resting(order_id, "canceled")

        adapter.cancel_order.side_effect = cancel

        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")

        adapter.cancel_order.assert_called_once()
        assert trader._halt_cancels_pending is True
        [halt] = _exactly(alerts, "Manual Halt")
        assert "cancelling tracked orders failed (OSError" in halt["message"]
        assert (
            f"awaiting cancel confirmation for AAA order_id={aaa.order_id}" not in (halt["message"])
        ), "the venue confirmed it"

    def test_a_restored_order_whose_report_cannot_be_applied_still_halts_at_startup(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        adapter = _resting_adapter()
        now = [TEST_CLOCK_NOW]
        trader, _, _ = _timeout_trader(adapter, store, now)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")

        trader, _, alerts = _timeout_trader(adapter, store, now)
        with pytest.raises(RuntimeError, match="not applied"):
            trader._initialize_run()

        assert trader._halted is True
        adapter.cancel_order.assert_called_once()
        [halt] = _exactly(alerts, "Order Reconciliation Failed")
        assert f"AAA order_id={aaa.order_id} broker report not applied" in halt["message"]


class TestAHaltCancelsEveryUnconfirmedOrder:
    def test_one_the_venue_reports_cancel_pending_is_cancelled(self):
        adapter = _resting_adapter()
        trader, alerts, _ = _trader(adapter)
        aaa = _buy_aaa(trader)
        # The venue started cancelling it on its own.
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        trader._advance_live_orders()
        assert aaa.status == "cancel_pending"
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")

        trader.halt("desk asked to stop")

        adapter.cancel_order.assert_called_once()
        assert adapter.cancel_order.call_args.args[0] == aaa.order_id
        assert trader._halt_cancels_pending is True
        [halt] = _exactly(alerts, "Manual Halt")
        assert f"awaiting cancel confirmation for AAA order_id={aaa.order_id}" in halt["message"]

        # The cancel it sent is pending, so it is not sent again.
        trader._poll_cycle()

        adapter.cancel_order.assert_called_once()
        assert _titled(alerts, "Halt Cancellation Completed") == []

        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
        trader._poll_cycle()

        assert trader._active_orders == []
        assert trader._halt_cancels_pending is False
        assert len(_titled(alerts, "Halt Cancellation Completed")) == 1

    def test_one_whose_cancel_the_venue_turned_down_is_cancelled_again(self):
        adapter = _resting_adapter()
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        trader, _, _ = _trader(adapter)
        aaa = _buy_aaa(trader)
        trader.halt("desk asked to stop")
        adapter.cancel_order.assert_called_once()

        # The venue refused the cancel; the order is working again.
        trader._poll_cycle()

        assert adapter.cancel_order.call_count == 2
        assert trader._active_orders == [aaa]
        assert trader._halt_cancels_pending is True

    @pytest.mark.parametrize("cancel_answers", [True, False])
    def test_one_whose_status_cannot_be_read_is_cancelled_by_its_order_id(self, cancel_answers):
        adapter = _resting_adapter()
        trader, alerts, _ = _trader(adapter)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = LookupError(f"IBKR order not found: {aaa.order_id}")
        if not cancel_answers:
            adapter.cancel_order.side_effect = LookupError(f"IBKR order not found: {aaa.order_id}")

        trader.halt("desk asked to stop")

        adapter.cancel_order.assert_called_once()
        assert adapter.cancel_order.call_args.args[0] == aaa.order_id
        [halt] = _exactly(alerts, "Manual Halt")
        if cancel_answers:
            assert trader._active_orders == []
            assert trader._halt_cancels_pending is False
            assert "awaiting cancel confirmation" not in halt["message"]
        else:
            assert trader._active_orders == [aaa]
            assert trader._halt_cancels_pending is True
            awaiting = f"awaiting cancel confirmation for AAA order_id={aaa.order_id}"
            assert awaiting in halt["message"]

    def test_after_a_restart_a_pending_cancel_goes_out_once_more_and_stays_owed(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        adapter = _resting_adapter()
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        now = [TEST_CLOCK_NOW]
        trader, _, _ = _timeout_trader(adapter, store, now)
        _buy_aaa(trader)
        trader.halt("desk asked to stop")
        adapter.cancel_order.assert_called_once()

        trader, _, alerts = _timeout_trader(adapter, store, now)
        trader._initialize_run()

        assert adapter.cancel_order.call_count == 2, "this process had not sent it"
        assert trader._halt_cancels_pending is True
        trader._poll_cycle()
        assert adapter.cancel_order.call_count == 2

        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
        trader._poll_cycle()

        assert trader._active_orders == []
        assert len(_titled(alerts, "Halt Cancellation Completed")) == 1

    def test_a_running_account_times_out_an_order_whose_venue_cancel_reverted(self):
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        trader._advance_live_orders()
        assert aaa.status == "cancel_pending"

        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id)
        now[0] += timedelta(seconds=30)
        trader._advance_live_orders()

        adapter.cancel_order.assert_called_once()
        assert adapter.cancel_order.call_args.args[0] == aaa.order_id
        assert trader._active_orders == []
        assert len(_exactly(alerts, "Order Timeout")) == 1


class TestAPartlyFilledOrderTheVenueIsCancelling:
    """A part fill does not hide a pending cancel, nor a fill past a risk limit."""

    def test_the_report_keeps_its_cancel_pending_status(self):
        trader, _, _ = _trader(_resting_adapter())
        aaa = _buy_aaa(trader)

        pending = LiveExecutor.normalize_report(
            aaa.request, _partly_filled(aaa.order_id, "PendingCancel")
        )
        whole = LiveExecutor.normalize_report(
            aaa.request, {**_partly_filled(aaa.order_id, "PendingCancel"), "filled": 1.0}
        )

        assert (pending.status, pending.filled_quantity) == ("cancel_pending", 0.5)
        assert whole.status == "filled"

    def test_a_halt_sends_its_cancel_once_while_the_venue_finishes_it(self):
        adapter = _resting_adapter()
        adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(order_id)
        adapter.cancel_order.side_effect = lambda order_id, _s: _partly_filled(
            order_id, "PendingCancel"
        )
        trader, alerts, _ = _trader(adapter)
        aaa = _buy_aaa(trader)
        trader.halt("desk asked to stop")
        adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(
            order_id, "PendingCancel"
        )

        for _ in range(3):
            trader._poll_cycle()

        adapter.cancel_order.assert_called_once()
        assert aaa.status == "cancel_pending"
        assert trader._halt_cancels_pending is True

        adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(order_id, "canceled")
        trader._poll_cycle()

        assert trader._active_orders == []
        assert trader._positions["AAA"].quantity == pytest.approx(0.5)
        assert len(_titled(alerts, "Halt Cancellation Completed")) == 1


def _entry(trader, *, grouped: bool):
    """Leave an AAA entry working, with a BBB leg of its group queued behind it if grouped."""
    group_id = "pair" if grouped else None
    trader._execute_live_decision(
        [
            OrderIntent(action="long", symbol="AAA", quantity=1.0, group_id=group_id),
            *(
                [OrderIntent(action="long", symbol="BBB", quantity=1.0, group_id=group_id)]
                if grouped
                else []
            ),
        ],
        _bars(),
        TEST_CLOCK_NOW,
    )
    return trader._active_orders[0]


class TestAFillPastARiskLimitHaltsWhicheverReportCarriesIt:
    """An entry fill is checked against the limits whether a status read or a cancel's answer brings it."""

    @pytest.mark.parametrize(
        ("path", "grouped"),
        [
            pytest.param("venue_pending_cancel", False, id="read-pending-cancel"),
            pytest.param("venue_cancelled", True, id="read-cancelled-grouped-leg"),
            pytest.param("timeout_cancel_filled", False, id="timeout-cancel-answer-filled"),
            pytest.param("timeout_cancel_cancelled", True, id="timeout-cancel-answer-grouped-leg"),
            pytest.param("restored_cancel", False, id="restored-cancel-answer"),
        ],
    )
    def test_it_halts_as_a_post_fill_risk_breach(self, path, grouped):
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        aaa = _entry(trader, grouped=grouped)
        trader._risk_policy = RiskPolicy(max_position_weight=0.0001)
        adapter.cancel_order.side_effect = lambda order_id, _s: _partly_filled(order_id, "canceled")
        if path == "venue_pending_cancel":
            adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(
                order_id, "PendingCancel"
            )
        elif path == "venue_cancelled":
            adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(
                order_id, "canceled"
            )
        elif path == "timeout_cancel_filled":
            adapter.cancel_order.side_effect = lambda order_id, _s: _broker_report(
                order_id=order_id, quantity=1.0
            )
            now[0] += timedelta(seconds=30)
        elif path == "timeout_cancel_cancelled":
            now[0] += timedelta(seconds=30)
        else:
            aaa.cancel_requested = True

        trader._poll_cycle()

        assert aaa.filled_quantity > 0
        assert trader._halted is True
        [breach] = _exactly(alerts, "Post-fill Risk Breach")
        assert "AAA confirmed weight" in breach["message"]
        assert f"after AAA order_id={aaa.order_id} status={aaa.status}" in breach["message"]
        assert _exactly(alerts, "Order Cancelled") == []
        assert _exactly(alerts, "Order Timeout") == []
        assert _submitted(adapter) == ["AAA"], "nothing after it went out"

    def test_a_flatten_that_books_one_still_closes_and_halts_once(self):
        adapter = _resting_adapter()
        trader, alerts, _ = _trader(adapter)
        trader._positions = {"BBB": _position("BBB")}
        aaa = _entry(trader, grouped=False)
        trader._risk_policy = RiskPolicy(max_position_weight=0.0001)
        adapter.cancel_order.side_effect = lambda order_id, _s: _partly_filled(order_id, "canceled")
        adapter.place_order.side_effect = lambda signal: _broker_report(
            order_id=signal["client_order_id"], quantity=signal["quantity"]
        )
        trader.request_flatten("desk asked to close")

        trader._poll_cycle()

        assert aaa.filled_quantity == pytest.approx(0.5)
        assert trader._halted is True
        assert trader._positions == {}
        assert [alert["title"] for alert in alerts] == ["[test] Operator Flatten"]
