"""A target-rebalance leg that fails goes out at most once per bar, and not on every bar.

A leg the venue refuses, cancels or leaves past its timeout stops its
rebalance, and no decision on the bar it was planned on replans it. That holds
when the store fails while the failure is recorded, which fails the cycle and
so retries the bar, and across a restart once the stop is written. The failure
alerts once, whether or not its checkpoint lands. Once the same symbol's leg
ends unfilled on two target rebalances in a row, with no fill of the symbol
between them, the account halts instead of sending the leg on every bar.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from librae.live.state import StateStoreUnavailableError

from tests.engine.test_halt_runtime_events import (
    _continue_on_the_next_bar,
    _next_bar,
    _of_type,
    _refusing_aaa,
    _SameTargets,
)
from tests.engine.test_live_flatten import (
    SYMBOLS,
    _adapter,
    _position,
    _submitted,
    _titled,
    _trader,
)
from tests.engine.test_live_runner import _broker_report, _make_ohlcv_at

OUTAGE = "state store unavailable"


def _paired(adapter, *, group_id: str | None = "pair"):
    """A trader continuing a rebalance that closes AAA, then BBB, on the next bar."""
    strategy = _SameTargets({"BBB": 0.0})
    trader, events, alerts = _trader(adapter, strategy=strategy)
    bar_times = _continue_on_the_next_bar(trader, strategy.weights)
    _hold_pair(trader, group_id)
    return trader, strategy, events, alerts, bar_times


def _hold_pair(trader, group_id: str | None = "pair") -> None:
    trader._positions = {
        symbol: replace(_position(symbol), group_id=group_id) for symbol in ("AAA", "BBB")
    }


def _restarted(trader, adapter, strategy):
    restored, events, alerts = _trader(adapter, strategy=strategy, state_store=trader._state_store)
    _hold_pair(restored)
    return restored, events, alerts


def _retry_bar(trader, bar_times) -> None:
    """Process the last bar again, as the poll does after a cycle that failed on it."""
    trader._process_cycle({symbol: _make_ohlcv_at(bar_times) for symbol in SYMBOLS}, bar_times[-1])


def _outage_from(trader, starts) -> dict:
    """Fail every save from the first that ``starts(state, orders)`` picks while ``down`` is set."""
    store = trader._state_store
    save = store.save
    outage = {"down": False, "began": False}

    def guarded_save(state, orders=()):
        if not outage["began"] and starts(state, orders):
            outage.update(began=True, down=True)
        if outage["down"]:
            raise StateStoreUnavailableError(OUTAGE)
        save(state, orders)

    store.save = guarded_save
    return outage


def _aaa_rows(trader) -> list:
    return [row for row in trader._state_store.orders.values() if row.request.symbol == "AAA"]


class TestTheBarAFailedLegWentOutOnIsNotDecidedAgain:
    def test_an_outage_at_the_refused_row_still_stops_and_alerts(self):
        """The row write raised before the stop, so the retried bar replanned and resent the leg."""
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa("rejected")
        trader, strategy, events, alerts, bar_times = _paired(adapter)
        outage = _outage_from(
            trader, lambda _state, orders: any(row.status == "rejected" for row in orders)
        )

        with pytest.raises(StateStoreUnavailableError):
            _next_bar(trader, bar_times)
        [alert] = _titled(alerts, "Order Rejected")
        assert "AAA" in alert["message"]
        assert "target rebalance stopped" in alert["message"]
        assert OUTAGE in alert["message"]
        with pytest.raises(StateStoreUnavailableError):
            _retry_bar(trader, bar_times)
        outage["down"] = False
        _retry_bar(trader, bar_times)
        trader._offer_held_runtime_events()  # as the next poll does first

        assert _submitted(adapter) == ["AAA"]
        assert strategy.calls == 0
        assert len(_titled(alerts, "Order Rejected")) == 1
        assert len(_of_type(events, "group_failed")) == 1
        [row] = _aaa_rows(trader)
        assert row.status == "rejected"
        assert trader._state_store.load(trader._state_key).live_rebalance is None

    @pytest.mark.parametrize("outcome", ["rejected", "cancelled"])
    def test_a_stop_whose_checkpoint_fails_still_alerts_and_holds_its_bar(self, outcome):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa(outcome)
        trader, strategy, events, alerts, bar_times = _paired(adapter)
        outage = _outage_from(trader, lambda state, _orders: state.live_rebalance is None)

        with pytest.raises(StateStoreUnavailableError):
            _next_bar(trader, bar_times)
        [alert] = _titled(alerts, f"Order {outcome.title()}")
        assert "AAA" in alert["message"]
        assert f"not recorded yet (StateStoreUnavailableError: {OUTAGE})" in alert["message"]
        outage["down"] = False
        _retry_bar(trader, bar_times)
        trader._offer_held_runtime_events()

        assert _submitted(adapter) == ["AAA"]
        assert strategy.calls == 0
        assert len(_of_type(events, "group_failed")) == 1
        assert trader._state_store.load(trader._state_key).live_rebalance is None

    def test_an_unfunded_stop_whose_checkpoint_fails_still_alerts_and_holds_its_bar(self):
        adapter = _adapter(below_minimum="")
        adapter.get_balance.return_value = {"free": 0.0, "used": 0.0, "total": 0.0}
        strategy = _SameTargets({"AAA": 0.5})
        trader, events, alerts = _trader(adapter, strategy=strategy)
        trader._positions = {}
        bar_times = _continue_on_the_next_bar(trader, strategy.weights)
        outage = _outage_from(trader, lambda state, _orders: state.live_rebalance is None)

        with pytest.raises(StateStoreUnavailableError):
            _next_bar(trader, bar_times)
        assert len(_titled(alerts, "Insufficient Free Balance")) == 1
        assert len(_of_type(events, "decision_skipped")) == 1
        outage["down"] = False
        _retry_bar(trader, bar_times)

        adapter.place_order.assert_not_called()
        assert strategy.calls == 0
        assert len(_of_type(events, "decision_skipped")) == 1

    def test_a_leg_a_failed_write_left_queued_holds_its_bar_once_refused(self):
        """The next cycle's order poll sends it, outside the rebalance that queued it."""
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa("rejected")
        trader, strategy, events, _, bar_times = _paired(adapter)
        outage = _outage_from(
            trader, lambda _state, orders: any(not row.placement_attempted for row in orders)
        )
        with pytest.raises(StateStoreUnavailableError):
            _next_bar(trader, bar_times)
        assert _submitted(adapter) == []
        outage["down"] = False

        trader._advance_live_orders()  # the next cycle's order poll
        _retry_bar(trader, bar_times)

        assert _submitted(adapter) == ["AAA"]
        assert strategy.calls == 0
        assert len(_of_type(events, "group_failed")) == 1

    def test_an_ungrouped_refusal_whose_row_write_fails_still_halts(self):
        """Its halt was skipped and the leg forgotten, with no alert."""
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa("rejected")
        trader, strategy, _, alerts, bar_times = _paired(adapter, group_id=None)
        outage = _outage_from(
            trader, lambda _state, orders: any(row.status == "rejected" for row in orders)
        )

        with pytest.raises(StateStoreUnavailableError):
            _next_bar(trader, bar_times)
        outage["down"] = False
        _retry_bar(trader, bar_times)

        assert trader._halted is True
        [alert] = _titled(alerts, "Order Rejected")
        assert "halt not persisted" in alert["message"]
        assert _submitted(adapter) == ["AAA"]
        assert strategy.calls == 0

    def test_a_restart_on_the_stopped_bar_does_not_decide_it(self):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa("rejected")
        trader, strategy, _, _, bar_times = _paired(adapter)
        _next_bar(trader, bar_times)

        restored, _, _ = _restarted(trader, adapter, strategy)
        _retry_bar(restored, bar_times)

        assert _submitted(adapter) == ["AAA"]
        assert strategy.calls == 0


class TestALegFailingOnTheNextRebalanceToo:
    @pytest.mark.parametrize("outcome", ["rejected", "cancelled"])
    def test_halts_instead_of_sending_it_every_bar(self, outcome):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa(outcome)
        trader, strategy, events, alerts, bar_times = _paired(adapter)
        title = f"Order {outcome.title()}"

        _next_bar(trader, bar_times)
        [stopped] = _titled(alerts, title)
        assert "target rebalance stopped" in stopped["message"]
        _next_bar(trader, bar_times)
        _next_bar(trader, bar_times)

        assert _submitted(adapter) == ["AAA", "AAA"]
        assert strategy.calls == 1
        assert trader._halted is True
        [_, halted] = _titled(alerts, title)
        assert "failed on the previous target rebalance too" in halted["message"]
        assert "trading halted" in halted["message"]
        assert len(_of_type(events, "group_failed")) == 1
        [event] = _of_type(events, "halted")
        assert event.detail["reason"] == title

    def test_a_restart_between_the_failures_still_halts(self):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa("rejected")
        trader, strategy, _, _, bar_times = _paired(adapter)
        _next_bar(trader, bar_times)

        restored, _, _ = _restarted(trader, adapter, strategy)
        _next_bar(restored, bar_times)

        assert _submitted(adapter) == ["AAA", "AAA"]
        assert restored._halted is True

    def test_a_fill_of_the_symbol_between_the_failures_starts_over(self):
        """A partial fill shows the venue still trades the symbol."""
        adapter = _adapter(below_minimum="")
        outcomes = iter(["rejected", "partial", "rejected", "rejected"])

        def place_order(signal):
            if signal["canonical_symbol"] != "AAA":
                return _broker_report(
                    order_id=signal["client_order_id"], quantity=signal["quantity"]
                )
            if next(outcomes) == "partial":
                return _broker_report(
                    order_id=signal["client_order_id"],
                    status="canceled",
                    quantity=signal["quantity"],
                    filled=signal["quantity"] / 2,
                )
            return _refusing_aaa("rejected")(signal)

        adapter.place_order.side_effect = place_order
        trader, _, events, _, bar_times = _paired(adapter)

        for _ in range(3):
            _next_bar(trader, bar_times)
        assert trader._halted is False
        assert len(_of_type(events, "group_failed")) == 3
        _next_bar(trader, bar_times)

        assert _submitted(adapter) == ["AAA"] * 4
        assert trader._halted is True
