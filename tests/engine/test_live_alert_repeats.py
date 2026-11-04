"""A problem that lasts alerts when it starts, reminds while it lasts, and says when it ends.

An alert that would otherwise go out every cycle goes out when its problem
first appears or changes, then once an hour while it lasts, saying for how
long and how often it was seen, and once as ``Resolved`` when it has ended:
for an order report, once its answers have applied for an hour, so one that
comes and goes stays one problem. A flatten that waits one cycle for the
venue to confirm its cancels is normal and does not alert. Only alerts
change: whether an order is cancelled, the account halted or a flatten held
back does not.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import OrderIntent
from librae.live.persistent_alerts import PersistentAlerts
from librae.live.state import MemoryLiveStateStore

from tests.engine.live_factories import (
    TEST_CLOCK_NOW,
    _HoldStrategy,
    _test_cfg,
    make_live_trader,
    with_run_leases,
)
from tests.engine.test_halt_runtime_events import _timeout_trader
from tests.engine.test_live_flatten import SYMBOLS, _bars, _position, _submitted
from tests.engine.test_live_halt_durability import _partly_filled, _resting_adapter, _unappliable
from tests.engine.test_live_operator_halt import _resting

HOUR = timedelta(hours=1)
NOT_APPLIED = "Order Report Not Applied"
WAITING = "Operator Flatten Waiting"


def _trader(adapter):
    """A trader without an order timeout, the default, and its clock."""
    now = [TEST_CLOCK_NOW]
    alerts: list[dict] = []
    trader = make_live_trader(
        _HoldStrategy(),
        adapter,
        config=_test_cfg(
            mode="live",
            symbols=SYMBOLS,
            execution=ExecutionPolicy(max_bar_volume_participation_rate=None, warmup_periods=5),
        ),
        state_store=MemoryLiveStateStore(restart_durable_for_tests=True),
        clock=lambda: now[0],
    )
    trader._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
    trader._last_prices = {symbol: 100.0 for symbol in SYMBOLS}
    return with_run_leases(trader), alerts, now


def _buy_aaa(trader):
    trader._execute_live_decision(
        [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
    )
    [aaa] = trader._active_orders
    return aaa


def _exactly(alerts, title: str) -> list[dict]:
    return [alert for alert in alerts if alert["title"].endswith(f"] {title}")]


def _flatten_waiting_on_aaa(cancel_status: str):
    """A flatten whose cancel of a resting AAA buy the venue answers with ``cancel_status``."""
    adapter = _resting_adapter()
    trader, alerts, now = _trader(adapter)
    trader._positions = {"AAA": _position("AAA")}
    aaa = _buy_aaa(trader)
    adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, cancel_status)
    trader.request_flatten("desk asked to close")
    return adapter, trader, alerts, now, aaa


class TestAFlattenWaitingForCancelConfirmation:
    @pytest.mark.parametrize(
        "cancel_status",
        [
            pytest.param("pending_cancel", id="ibkr-pending-cancel"),
            pytest.param("open", id="shioaji-still-submitted"),
        ],
    )
    def test_one_the_venue_confirms_a_cycle_later_does_not_alert(self, cancel_status, caplog):
        adapter, trader, alerts, _, aaa = _flatten_waiting_on_aaa(cancel_status)

        with caplog.at_level(logging.INFO):
            trader._poll_cycle()

            assert trader._active_orders == [aaa], "the venue has not confirmed the cancel"
            assert trader._halted is False
            assert _exactly(alerts, WAITING) == []

            adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
            trader._poll_cycle()

        assert trader._halted is True
        assert len(_exactly(alerts, "Operator Flatten")) == 1
        assert _submitted(adapter) == ["AAA", "AAA"], "the entry, then the flatten's exit"
        assert _exactly(alerts, WAITING) == []
        assert _exactly(alerts, "Resolved") == [], "nothing was alerted, so nothing resolves"
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert errors == [], "a wait that ends by itself is not an error"

    def test_one_still_waiting_on_its_second_cycle_alerts_reminds_and_resolves(self):
        adapter, trader, alerts, now, aaa = _flatten_waiting_on_aaa("pending_cancel")

        trader._poll_cycle()
        assert _exactly(alerts, WAITING) == []

        # The venue keeps reporting the cancel pending.
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        trader._poll_cycle()
        [waiting] = _exactly(alerts, WAITING)
        assert f"AAA order_id={aaa.order_id} (cancel_pending)" in waiting["message"]

        for _ in range(3):
            now[0] += timedelta(minutes=10)
            trader._poll_cycle()
        assert len(_exactly(alerts, WAITING)) == 1, "the same wait is not alerted every cycle"

        now[0] += HOUR
        trader._poll_cycle()
        _, reminder = _exactly(alerts, WAITING)
        assert f"AAA order_id={aaa.order_id} (cancel_pending)" in reminder["message"]
        assert "still so after 1h 30m, seen 5 times" in reminder["message"]
        assert trader._halted is False
        assert adapter.cancel_order.call_count == 1, "a cancel the venue reports pending stands"

        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
        trader._poll_cycle()

        [resolved] = _exactly(alerts, "Resolved")
        assert resolved["message"].startswith(f"{WAITING} cleared after 1h 30m, seen 5 times")
        assert alerts.index(resolved) < alerts.index(_exactly(alerts, "Operator Flatten")[0])
        assert trader._halted is True
        assert _submitted(adapter) == ["AAA", "AAA"]

    def test_one_refused_by_a_halt_while_waiting_is_not_resolved(self):
        """The refusal says how the wait ended: a resolved one would read as a done flatten."""
        adapter, trader, alerts, _, _ = _flatten_waiting_on_aaa("pending_cancel")
        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        trader._poll_cycle()
        trader._poll_cycle()
        assert len(_exactly(alerts, WAITING)) == 1

        trader.request_halt("desk asked to stop")
        trader._poll_cycle()

        assert trader._flatten_request is None
        assert len(_exactly(alerts, "Operator Flatten Refused")) == 1
        assert _exactly(alerts, "Resolved") == []
        assert _submitted(adapter) == ["AAA"], "only the entry"


class TestAReportThatKeepsFailingToApply:
    def _polled_until_it_fails(self):
        adapter = _resting_adapter()
        adapter.place_order.side_effect = lambda signal: _partly_filled(signal["client_order_id"])
        trader, alerts, now = _trader(adapter)
        aaa = _buy_aaa(trader)
        return adapter, trader, alerts, now, aaa

    def test_without_an_order_timeout_it_is_alerted_again_while_it_lasts(self):
        adapter, trader, alerts, now, aaa = self._polled_until_it_fails()
        adapter.get_order.side_effect = lambda order_id, _s: _unappliable("order_id", order_id)

        for _ in range(25):
            trader._poll_cycle()
            now[0] += timedelta(minutes=5)

        first, one_hour, two_hours = _titled_not_applied(alerts)
        assert f"AAA order_id={aaa.order_id} broker report not applied" in first["message"]
        assert "still so after" not in first["message"]
        assert "still so after 1h 0m, seen 13 times" in one_hour["message"]
        assert "still so after 2h 0m, seen 25 times" in two_hours["message"]
        assert f"AAA order_id={aaa.order_id} broker report not applied" in two_hours["message"]
        assert trader._halted is False
        adapter.cancel_order.assert_not_called()
        assert trader._active_orders == [aaa], "it stays tracked and blocks the strategy"

    def test_a_changed_mismatch_alerts_at_once_even_back_to_an_earlier_one(self):
        adapter, trader, alerts, _, _ = self._polled_until_it_fails()

        for mismatch in ("order_id", "filled_backwards", "order_id"):
            adapter.get_order.side_effect = lambda order_id, _s, m=mismatch: _unappliable(
                m, order_id
            )
            trader._poll_cycle()

        first, second, third = _titled_not_applied(alerts)
        assert "order id changed" in first["message"]
        assert "filled quantity moved backwards" in second["message"]
        assert "order id changed" in third["message"]
        assert trader._halted is False

    def test_one_that_applies_again_for_an_hour_is_resolved_once(self):
        adapter, trader, alerts, now, aaa = self._polled_until_it_fails()
        adapter.get_order.side_effect = lambda order_id, _s: _unappliable("order_id", order_id)
        trader._poll_cycle()
        now[0] += timedelta(minutes=3)
        trader._poll_cycle()

        adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(order_id)
        trader._poll_cycle()
        now[0] += HOUR - timedelta(seconds=1)
        trader._poll_cycle()
        assert _exactly(alerts, "Resolved") == [], "not yet clear for an hour"

        now[0] += timedelta(seconds=1)
        trader._poll_cycle()
        trader._poll_cycle()

        assert len(_titled_not_applied(alerts)) == 1
        [resolved] = _exactly(alerts, "Resolved")
        assert resolved["message"].startswith(f"{NOT_APPLIED} cleared after 3m 0s, seen 2 times")
        assert f"AAA order_id={aaa.order_id} broker report not applied" in resolved["message"]
        assert trader._active_orders == [aaa], "a working order stays tracked"

        adapter.get_order.side_effect = lambda order_id, _s: _unappliable("order_id", order_id)
        trader._poll_cycle()
        assert len(_titled_not_applied(alerts)) == 2, "a resolved problem that returns is new"

    def test_one_that_fails_every_other_read_stays_one_problem(self):
        adapter, trader, alerts, now, _ = self._polled_until_it_fails()
        answers = [
            lambda order_id, _s: _unappliable("filled_backwards", order_id),
            lambda order_id, _s: _partly_filled(order_id),
        ]

        for cycle in range(24):
            adapter.get_order.side_effect = answers[cycle % 2]
            trader._poll_cycle()
            now[0] += timedelta(minutes=5)

        first, reminder = _titled_not_applied(alerts)
        assert "still so" not in first["message"]
        assert "still so after 1h 0m, seen 7 times" in reminder["message"]
        assert _exactly(alerts, "Resolved") == []
        assert trader._halted is False

    def test_one_a_halt_owes_a_cancel_for_repeats_and_resolves_once_it_ends(self):
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        _buy_aaa(trader)
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "pending_cancel")
        trader.halt("desk asked to stop")
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")

        for _ in range(13):
            trader._poll_cycle()
            now[0] += timedelta(minutes=5)

        first, reminder = _titled_not_applied(alerts)
        assert "cancel already pending; the halt's cancellation stays owed" in first["message"]
        assert "still so after 1h 0m, seen 13 times" in reminder["message"]
        adapter.cancel_order.assert_called_once()
        assert trader._halt_cancels_pending is True

        adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
        trader._poll_cycle()
        assert trader._active_orders == []
        assert len(_exactly(alerts, "Halt Cancellation Completed")) == 1

        trader._poll_cycle()
        [resolved] = _exactly(alerts, "Resolved")
        assert resolved["message"].startswith(f"{NOT_APPLIED} cleared after 1h 5m, seen 13 times")

    def test_a_cancel_error_whose_text_differs_every_time_stays_one_problem(self):
        """A venue error names its own request, so its text is new on every cancel."""
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        _buy_aaa(trader)
        attempts = iter(range(1, 100))

        def cancel_times_out(_order_id, _s):
            raise RuntimeError(f"request timed out (request id {next(attempts)})")

        adapter.cancel_order.side_effect = cancel_times_out
        trader.halt("desk asked to stop")
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")

        for _ in range(13):
            trader._poll_cycle()
            now[0] += timedelta(minutes=5)

        first, reminder = _titled_not_applied(alerts)
        assert "cancel not confirmed (RuntimeError: request timed out" in first["message"]
        assert "still so after 1h 0m, seen 13 times" in reminder["message"]
        assert first["message"] != reminder["message"].split(" — still so")[0]
        assert trader._halt_cancels_pending is True

    def test_one_whose_order_ends_in_the_pass_a_read_failed_is_not_resolved(self):
        """Its cancel's answer applied, but the read's fills may be missing from the ledger."""
        adapter = _resting_adapter()
        trader, alerts, _ = _trader(adapter)
        trader._positions = {"AAA": _position("AAA")}
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")
        trader.request_flatten("desk asked to close")

        trader._poll_cycle()
        assert aaa not in trader._active_orders, "its cancel's own answer ended it"
        assert trader._halted is True
        trader._poll_cycle()

        named = f"AAA order_id={aaa.order_id} broker report not applied"
        [alert] = [alert for alert in _titled_not_applied(alerts) if named in alert["message"]]
        assert alert["message"].endswith("cancel sent")
        assert [alert for alert in _exactly(alerts, "Resolved") if named in alert["message"]] == []

    def test_one_cancelled_at_its_timeout_in_the_pass_its_read_failed_is_not_resolved(self):
        adapter = _resting_adapter()
        now = [TEST_CLOCK_NOW]
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, _, alerts = _timeout_trader(adapter, store, now)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda _order_id, _s: _resting("another-order")
        now[0] += timedelta(seconds=30)

        trader._poll_cycle()
        assert aaa not in trader._active_orders, "its timeout's cancel ended it"
        assert len(_exactly(alerts, "Order Timeout")) == 1
        trader._poll_cycle()

        assert len(_titled_not_applied(alerts)) == 1
        assert _exactly(alerts, "Resolved") == [], "its status read never applied again"

    def _clear_for_an_hour_then(self, answer):
        """An order whose problem has been clear for an hour, read next as ``answer``,
        while the notifier refuses the Resolved alert that read sends."""
        adapter, trader, alerts, now, aaa = self._polled_until_it_fails()
        adapter.get_order.side_effect = lambda order_id, _s: _unappliable("order_id", order_id)
        trader._poll_cycle()
        adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(order_id)
        trader._poll_cycle()
        now[0] += HOUR
        adapter.get_order.side_effect = answer
        notify = trader._notify

        def refuse_resolved(method, **kw):
            if kw.get("title", "").endswith("] Resolved"):
                raise RuntimeError("notifier down")
            notify(method, **kw)

        trader._notify = refuse_resolved
        trader._poll_cycle()
        return trader, alerts, aaa

    def test_a_resolved_alert_that_fails_to_send_leaves_the_fill_booked_once(self):
        trader, _, aaa = self._clear_for_an_hour_then(
            lambda order_id, _s: _partly_filled(order_id, filled=0.75)
        )

        assert aaa.filled_quantity == pytest.approx(0.75)
        assert trader._positions["AAA"].quantity == pytest.approx(0.75)
        trader._poll_cycle()
        assert trader._positions["AAA"].quantity == pytest.approx(0.75), "booked once"

    def test_a_resolved_alert_that_fails_to_send_does_not_skip_the_halt(self):
        trader, alerts, aaa = self._clear_for_an_hour_then(
            lambda order_id, _s: _partly_filled(order_id, "rejected")
        )

        assert aaa not in trader._active_orders
        assert trader._halted is True, "an ungrouped order's rejection halts"
        [rejected] = _exactly(alerts, "Order Rejected")
        assert f"AAA order_id={aaa.order_id}" in rejected["message"]

    def test_one_open_when_a_halt_ends_the_order_on_its_cancel_is_not_resolved(self):
        """The halt's read still fails, so a fill it claims is not booked: nothing cleared."""
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda order_id, _s: _unappliable("order_id", order_id)
        for _ in range(2):
            trader._poll_cycle()
            now[0] += timedelta(minutes=5)

        trader.request_halt("desk asked to stop")
        for _ in range(2):
            trader._poll_cycle()
            now[0] += timedelta(minutes=5)

        assert trader._halted is True
        assert aaa not in trader._active_orders, "its cancel's own answer ended it"
        assert aaa.filled_quantity == 0.0
        assert len(_exactly(alerts, "Halt Cancellation Completed")) == 1
        assert _exactly(alerts, "Resolved") == []

    def test_one_seen_clear_then_failing_in_the_halt_that_ends_it_is_not_resolved(self):
        adapter, trader, alerts, now, aaa = self._polled_until_it_fails()
        failing = lambda order_id, _s: _unappliable("filled_backwards", order_id)  # noqa: E731
        adapter.get_order.side_effect = failing
        trader._poll_cycle()
        now[0] += timedelta(minutes=5)
        adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(order_id)
        trader._poll_cycle()
        now[0] += timedelta(minutes=5)

        adapter.get_order.side_effect = failing
        adapter.cancel_order.side_effect = lambda order_id, _s: _partly_filled(order_id, "canceled")
        trader.request_halt("desk asked to stop")
        for _ in range(2):
            trader._poll_cycle()
            now[0] += timedelta(minutes=5)

        assert trader._halted is True
        assert aaa not in trader._active_orders
        assert _exactly(alerts, "Resolved") == [], "the halt's read failed again"

    def test_one_whose_failure_a_halt_only_logs_is_not_resolved_while_it_fails(self):
        """A halt's cancel pass raises its first failure; a later order's is only logged.

        Two placed orders are built directly: that later order's reads keep
        failing while its cancel's answers apply, so it must stay open.
        """
        adapter = _resting_adapter()
        trader, alerts, now = _trader(adapter)
        trader._execute_live_decision(
            [
                OrderIntent(action="long", symbol="AAA", quantity=1.0),
                OrderIntent(action="long", symbol="BBB", quantity=1.0),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )
        aaa, bbb = trader._active_orders
        adapter.get_order.side_effect = lambda order_id, _s: _unappliable("order_id", order_id)
        trader._poll_cycle()
        assert len(_titled_not_applied(alerts)) == 1, "AAA, the polled head"
        bbb.placement_attempted = True
        bbb.placement_attempted_at = TEST_CLOCK_NOW
        bbb.order_id = bbb.request.client_order_id
        bbb.status = "accepted"
        trader._active_orders = [bbb, aaa]
        # A venue with no pending-cancel state: each pass sends the cancel again.
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id)

        with pytest.raises(RuntimeError, match=r"BBB order_id=.* not applied"):
            trader.halt("desk asked to stop")
        for _ in range(8):
            now[0] += timedelta(minutes=10)
            trader._poll_cycle()

        assert trader._active_orders == [bbb, aaa], "neither cancel is confirmed"
        named = f"AAA order_id={aaa.order_id}"
        assert [alert for alert in _exactly(alerts, "Resolved") if named in alert["message"]] == []

    def test_one_whose_timeout_cancel_answer_does_not_apply_is_not_resolved_by_the_halt(self):
        """The halt that follows reads the order ended, in a pass after the failed answer."""
        adapter = _resting_adapter()
        adapter.place_order.side_effect = lambda signal: _partly_filled(signal["client_order_id"])
        now = [TEST_CLOCK_NOW]
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, _, alerts = _timeout_trader(adapter, store, now)
        aaa = _buy_aaa(trader)
        adapter.get_order.side_effect = lambda order_id, _s: _unappliable("order_id", order_id)
        trader._poll_cycle()
        adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(order_id)
        trader._poll_cycle()

        now[0] += timedelta(seconds=30)
        adapter.cancel_order.side_effect = lambda _order_id, _s: _partly_filled(
            "another-order", "canceled"
        )
        reads = iter(["open", "canceled"])
        adapter.get_order.side_effect = lambda order_id, _s: _partly_filled(order_id, next(reads))
        trader._poll_cycle()
        assert trader._halted is True
        assert len(_exactly(alerts, "Order Timeout Cancellation Failed")) == 1
        assert aaa not in trader._active_orders, "the halt's read found it cancelled"
        trader._poll_cycle()

        assert _exactly(alerts, "Resolved") == []


def _titled_not_applied(alerts) -> list[dict]:
    return _exactly(alerts, NOT_APPLIED)


class TestARepeatBetweenAlerts:
    def _alerts(self):
        now = [TEST_CLOCK_NOW]
        sent: list[tuple[str, str]] = []
        alerts = PersistentAlerts(
            lambda title, message: sent.append((title, message)), lambda: now[0]
        )
        return alerts, sent, now

    def test_is_logged_at_the_default_level(self, caplog):
        alerts, sent, _ = self._alerts()
        alerts.fire("key", "Stuck", "still stuck")
        caplog.clear()

        with caplog.at_level(logging.INFO, logger="librae.live.persistent_alerts"):
            alerts.fire("key", "Stuck", "still stuck")

        assert len(sent) == 1
        [record] = caplog.records
        assert record.levelno == logging.INFO
        assert "seen 2 times" in record.getMessage()

    def test_after_the_clock_steps_back_reports_no_negative_duration(self):
        alerts, sent, now = self._alerts()
        alerts.fire("key", "Stuck", "still stuck")
        now[0] -= 2 * HOUR

        alerts.resolve("key")

        [_, (title, message)] = sent
        assert title == "Resolved"
        assert message.startswith("Stuck cleared after 0s, seen once")
