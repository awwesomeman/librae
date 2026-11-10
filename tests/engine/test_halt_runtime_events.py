"""Halt alerts, resets and group failures leave a durable record, as does a failed placement.

Every halt alert records one ``halted`` event, so one stop can record several.
Each event is offered to ``on_runtime_event`` after the action it records, so
a slow or failing sink can neither delay it nor undo it: a sink that raises is
logged, and the halt still happens and still raises its own error. One recorded
while the checkpoint is failing is held until one lands. A failed
grouped leg also stops a target rebalance, which would otherwise send it
again in the same cycle, and so does one whose cancel ends in a later cycle,
after a restart or a pending acknowledgement. A placement's error text reaches its
``broker_orders`` row; a definite refusal is ``rejected``, while an unknown
outcome keeps its unresolved status because the order may still rest at the
venue.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from librae.core.run_config import ExecutionPolicy, RiskPolicy
from librae.core.strategy import OrderIntent, PortfolioWeights, Strategy
from librae.live.executor import OrderRejectedError
from librae.live.state import LiveRebalance, MemoryLiveStateStore

from tests.engine.live_factories import (
    TEST_CLOCK_NOW,
    _HoldStrategy,
    _test_cfg,
    make_live_trader,
    with_run_leases,
)
from tests.engine.test_live_flatten import (
    SYMBOLS,
    _adapter,
    _bars,
    _position,
    _submitted,
    _titled,
    _trader,
)
from tests.engine.test_live_halt_durability import OUTAGE, _resting_adapter, _store_outage
from tests.engine.test_live_operator_halt import _resting
from tests.engine.test_live_runner import _broker_report, _make_ohlcv_at, _mock_order_adapter


def _of_type(events, event_type: str) -> list:
    return [event for event in events if event.event_type == event_type]


def _failing_sink(event) -> None:
    raise RuntimeError("event sink down")


def _sink_on_the_store(trader, events: list, outage: dict) -> None:
    """A runtime-event sink that writes to the store, so it fails during its outage."""

    def sink(event) -> None:
        if outage["down"]:
            raise OSError(OUTAGE)
        events.append(event)

    trader._on_runtime_event = sink


def _live_trader(adapter, *symbols: str):
    events: list = []
    trader = make_live_trader(
        _HoldStrategy(),
        adapter,
        config=_test_cfg(mode="live", symbols=list(symbols)),
        on_runtime_event=events.append,
    )
    alerts: list[dict] = []
    trader._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
    trader._last_prices = {symbol: 100.0 for symbol in symbols}
    return trader, events, alerts


def _group_trader(adapter):
    return _live_trader(adapter, "SPOT", "PERP")


def _buy_btc(trader) -> None:
    trader._execute_live_decision(
        [OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0)],
        {"BTCUSDT": {"close": 100.0, "volume": 10_000.0}},
        TEST_CLOCK_NOW,
    )


def _basis_group() -> list[OrderIntent]:
    return [
        OrderIntent(action="long", symbol="SPOT", quantity=1.0, group_id="basis"),
        OrderIntent(action="short", symbol="PERP", quantity=1.0, group_id="basis"),
    ]


def _group_bars() -> dict[str, dict[str, float]]:
    return {symbol: {"close": 100.0, "volume": 10_000.0} for symbol in ("SPOT", "PERP")}


class _SameTargets(Strategy):
    """Asks for the same target book on every bar."""

    def __init__(self, weights: dict[str, float]) -> None:
        self.weights = weights
        self.calls = 0

    def on_bar(self, ctx):
        self.calls += 1
        return PortfolioWeights(self.weights)


def _continue_on_the_next_bar(trader, weights: dict[str, float]) -> list[datetime]:
    """Leave a target rebalance decided on the bar before ``TEST_CLOCK_NOW``.

    Returns the bar times, to which each later bar is appended.
    """
    decided = TEST_CLOCK_NOW - timedelta(hours=1)
    trader._live_rebalance = LiveRebalance(
        targets=PortfolioWeights(weights),
        reference_prices={symbol: 100.0 for symbol in SYMBOLS},
        reference_volumes={symbol: 10_000.0 for symbol in SYMBOLS},
        lagged_adv_by_symbol={},
        decided_at=decided,
    )
    trader._persist_state()
    return [decided]


def _next_bar(trader, bar_times: list[datetime]) -> None:
    bar_times.append(bar_times[-1] + timedelta(hours=1))
    trader._process_cycle({symbol: _make_ohlcv_at(bar_times) for symbol in SYMBOLS}, bar_times[-1])


def _refusing_aaa(outcome: str):
    """``place_order`` that fills every order but AAA's, which ends ``outcome``."""

    def place_order(signal):
        if signal["canonical_symbol"] != "AAA":
            return _broker_report(order_id=signal["client_order_id"], quantity=signal["quantity"])
        if outcome == "rejected":
            raise OrderRejectedError("ReduceOnly Order is rejected.")
        return _broker_report(
            order_id=signal["client_order_id"],
            status="canceled",
            quantity=signal["quantity"],
            filled=0.0,
        )

    return place_order


class TestEachHaltAlertIsRecorded:
    @pytest.mark.parametrize("path", ["halt", "request_halt"])
    def test_an_operator_halt_records_its_reason(self, path):
        trader, events, _ = _trader(_adapter())

        if path == "halt":
            trader.halt("desk asked to stop")
        else:
            trader.request_halt("desk asked to stop")
            trader._poll_cycle()

        [halted] = _of_type(events, "halted")
        assert halted.ts == TEST_CLOCK_NOW
        assert halted.symbol is None
        assert halted.detail == {"reason": "Manual Halt", "message": "desk asked to stop"}

    def test_an_engine_halt_records_its_title_and_cause(self):
        adapter = _adapter()
        adapter.prepare_order.side_effect = ValueError("AAA quantity exceeds maximum")
        trader, events, _ = _trader(adapter)

        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )

        [halted] = _of_type(events, "halted")
        assert halted.detail["reason"] == "Live Order Preflight Rejected"
        assert "AAA quantity exceeds maximum" in halted.detail["message"]

    def test_an_operator_flatten_records_its_reason_and_outcome(self):
        trader, events, _ = _trader(_adapter())

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        [halted] = _of_type(events, "halted")
        assert halted.detail["reason"] == "Operator Flatten"
        assert "desk asked to stop" in halted.detail["message"]
        assert "DUST" in halted.detail["message"], "names what remains open"

    def test_a_drawdown_breach_records_the_breach(self):
        trader, events, _ = _trader(_adapter())
        trader._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
        trader._cash = 0.0
        trader._equity_peak = 1_000.0

        trader._record_equity(TEST_CLOCK_NOW)

        [halted] = _of_type(events, "halted")
        assert halted.detail["reason"] == "Max Drawdown Breach"
        assert "drawdown=" in halted.detail["message"]

    def test_a_flatten_whose_exit_halts_records_both_alerts(self):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = TimeoutError("read timed out")
        trader, events, alerts = _trader(adapter)

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        reasons = [event.detail["reason"] for event in _of_type(events, "halted")]
        assert reasons == ["Ambiguous Order Placement", "Operator Flatten"]
        assert _titled(alerts, "Ambiguous Order Placement") and _titled(alerts, "Operator Flatten")

    def test_an_operator_halt_of_a_halted_account_records_nothing(self):
        """It changes nothing: its alert only reports the halt already in force."""
        trader, events, alerts = _trader(_adapter())
        trader.halt("first")

        trader.halt("second")

        assert len(_of_type(events, "halted")) == 1
        assert "already halted" in _titled(alerts, "Manual Halt")[-1]["message"]


class TestAFailingSinkNeverBreaksAHalt:
    def test_the_halt_still_persists_cancels_and_alerts(self, caplog):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )
        trader._on_runtime_event = _failing_sink

        with caplog.at_level(logging.ERROR, logger="librae.live.engine"):
            trader.halt("desk asked to stop")

        assert trader._halted is True
        assert trader._state_store.load(trader._state_key).halted is True
        adapter.cancel_order.assert_called_once()
        assert trader._active_orders == []
        assert _titled(alerts, "Manual Halt")
        assert "event sink down" in caplog.text

    def test_the_halt_still_raises_its_own_checkpoint_failure(self):
        trader, _, alerts = _trader(_resting_adapter())
        _store_outage(trader)
        trader._on_runtime_event = _failing_sink

        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")

        assert trader._halted is True
        [halt] = _titled(alerts, "Manual Halt")
        assert "not persisted" in halt["message"]

    def test_a_flatten_still_halts_and_alerts(self):
        adapter = _adapter(below_minimum="")
        trader, _, alerts = _trader(adapter)
        trader._on_runtime_event = _failing_sink

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        assert trader._halted is True
        assert trader._state_store.load(trader._state_key).halted is True
        assert trader._positions == {}
        [flatten] = _titled(alerts, "Operator Flatten")
        assert "flattened account positions" in flatten["message"]


class TestAnEventDuringAStoreOutageIsHeld:
    """Its sink usually writes to the store, so offering it then would drop it."""

    def test_a_halt_during_an_outage_is_recorded_once_the_store_recovers(self):
        trader, events, _ = _trader(_adapter())
        outage = _store_outage(trader)
        _sink_on_the_store(trader, events, outage)

        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")
        outage["down"] = False
        trader._poll_cycle()

        [halted] = _of_type(events, "halted")
        assert halted.ts == TEST_CLOCK_NOW
        assert halted.detail == {"reason": "Manual Halt", "message": "desk asked to stop"}
        assert trader._state_store.load(trader._state_key).halted is True

    def test_held_events_wait_while_the_store_stays_down(self):
        trader, events, _ = _trader(_adapter())
        outage = _store_outage(trader)

        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")
        with pytest.raises(OSError, match=OUTAGE):
            trader._poll_cycle()

        assert events == []
        outage["down"] = False
        trader._poll_cycle()
        assert [event.event_type for event in events] == ["halted"]

    def test_shutdown_offers_what_a_landed_checkpoint_released(self):
        trader, events, _ = _trader(_adapter())
        outage = _store_outage(trader)
        _sink_on_the_store(trader, events, outage)
        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")
        outage["down"] = False
        trader._persist_state()

        trader._close()

        assert [event.event_type for event in events] == ["halted"]

    def test_shutdown_names_what_a_store_still_down_drops(self, caplog):
        trader, events, _ = _trader(_adapter())
        outage = _store_outage(trader)
        _sink_on_the_store(trader, events, outage)
        with pytest.raises(OSError, match=OUTAGE):
            trader.halt("desk asked to stop")

        with caplog.at_level(logging.WARNING, logger="librae.live.engine"):
            trader._close()

        assert events == []
        assert f"halted at {TEST_CLOCK_NOW.isoformat()}" in caplog.text


class TestResetIsRecorded:
    def test_a_successful_reset_records_the_new_epoch(self):
        trader, events, _ = _trader(_adapter())
        trader._positions = {}
        trader.halt("desk asked to stop")

        trader.reset_halt()

        [reset] = _of_type(events, "halt_reset")
        assert reset.ts == TEST_CLOCK_NOW
        assert reset.symbol is None
        assert reset.detail == {
            "was_halted": True,
            "equity": pytest.approx(100_000.0),
            "persisted": True,
        }

    def test_a_reset_whose_checkpoint_fails_is_recorded_once_the_store_recovers(self):
        """It already applied in memory, so the process trades on it."""
        trader, events, _ = _trader(_adapter())
        trader._positions = {}
        trader.halt("desk asked to stop")
        outage = _store_outage(trader)
        _sink_on_the_store(trader, events, outage)

        with pytest.raises(OSError, match=OUTAGE):
            trader.reset_halt()
        assert trader._halted is False
        assert _of_type(events, "halt_reset") == []

        outage["down"] = False
        trader._persist_state()
        trader._poll_cycle()

        [reset] = _of_type(events, "halt_reset")
        assert reset.ts == TEST_CLOCK_NOW
        assert reset.detail["persisted"] is False
        assert reset.detail["was_halted"] is True

    def test_a_refused_reset_records_no_reset(self):
        trader, events, _ = _trader(_adapter())
        trader.halt("desk asked to stop")
        trader._last_bar_ts = {}

        with pytest.raises(RuntimeError, match="missing_valuation_mark"):
            trader.reset_halt()

        assert _of_type(events, "halt_reset") == []
        assert trader._halted is True

    def test_a_failing_sink_does_not_fail_a_reset_that_happened(self):
        trader, _, _ = _trader(_adapter())
        trader._positions = {}
        trader.halt("desk asked to stop")
        trader._on_runtime_event = _failing_sink

        trader.reset_halt()

        assert trader._halted is False
        assert trader._state_store.load(trader._state_key).halted is False


class TestGroupFailureIsRecorded:
    def test_a_refused_leg_records_its_group_without_a_halt(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = OrderRejectedError("Margin is insufficient.")
        trader, events, _ = _group_trader(adapter)

        complete = trader._execute_live_decision(_basis_group(), _group_bars(), TEST_CLOCK_NOW)

        assert complete is True
        assert trader._halted is False
        [failed] = _of_type(events, "group_failed")
        assert failed.ts == TEST_CLOCK_NOW
        assert failed.symbol == "SPOT"
        assert failed.detail["reason"] == "Order Rejected"
        assert failed.detail["group_id"] == "basis"
        assert failed.detail["client_order_id"]
        assert "Margin is insufficient." in failed.detail["message"]
        assert failed.detail["rebalance_stopped"] is False
        assert _of_type(events, "halted") == []

    def test_a_failing_sink_still_cancels_the_group(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = OrderRejectedError("Margin is insufficient.")
        trader, _, alerts = _group_trader(adapter)
        trader._on_runtime_event = _failing_sink

        complete = trader._execute_live_decision(_basis_group(), _group_bars(), TEST_CLOCK_NOW)

        assert complete is True
        assert trader._active_orders == []
        assert adapter.place_order.call_count == 1, "the sibling leg was cancelled unsent"
        assert any("'basis'" in alert["message"] for alert in alerts)

    def test_an_account_fault_records_a_halt_not_a_group_failure(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = OrderRejectedError("Invalid API-key.", account_fault=True)
        trader, events, _ = _group_trader(adapter)

        trader._execute_live_decision(_basis_group(), _group_bars(), TEST_CLOCK_NOW)

        assert _of_type(events, "group_failed") == []
        [halted] = _of_type(events, "halted")
        assert halted.detail["reason"] == "Order Rejected"

    @pytest.mark.parametrize("paused", [False, True])
    @pytest.mark.parametrize("outcome", ["rejected", "cancelled"])
    def test_a_failed_rebalance_leg_is_sent_once_and_stops_the_rebalance(
        self, outcome, paused, caplog
    ):
        """A target rebalance that kept going would replan the same leg and send it again."""
        adapter = _adapter(below_minimum="")

        def place_order(signal):
            if adapter.place_order.call_count > 3:
                raise RuntimeError("resubmitted within one cycle")
            if signal["canonical_symbol"] != "AAA":
                return _broker_report(
                    order_id=signal["client_order_id"], quantity=signal["quantity"]
                )
            if outcome == "rejected":
                raise OrderRejectedError("ReduceOnly Order is rejected.")
            return _broker_report(
                order_id=signal["client_order_id"],
                status="canceled",
                quantity=signal["quantity"],
                filled=0.0,
            )

        adapter.place_order.side_effect = place_order
        trader, events, alerts = _trader(adapter)
        if paused:
            trader.pause_entries("desk review")
        trader._positions = {
            symbol: replace(_position(symbol), group_id="pair") for symbol in ("AAA", "BBB")
        }

        complete = trader._execute_live_decision(
            PortfolioWeights({"BBB": 0.0}), _bars(), TEST_CLOCK_NOW
        )

        assert _submitted(adapter) == ["AAA"]
        assert complete is True
        assert trader._halted is False
        assert set(trader._positions) == {"AAA", "BBB"}
        assert trader._live_rebalance is None
        assert trader._state_store.load(trader._state_key).live_rebalance is None
        [failed] = _of_type(events, "group_failed")
        assert failed.detail["group_id"] == "pair"
        assert failed.detail["rebalance_stopped"] is True
        [alert] = _titled(alerts, f"Order {outcome.title()}")
        assert "target rebalance stopped" in alert["message"]
        [logged] = [
            record
            for record in caplog.records
            if record.name == "librae.live.engine" and record.levelname == "ERROR"
        ]
        assert logged.getMessage() == f"Order {outcome.title()}: {alert['message'][:-1]}"

    @pytest.mark.parametrize("restored", [False, True])
    @pytest.mark.parametrize("outcome", ["rejected", "cancelled"])
    def test_a_rebalance_stopped_while_continuing_is_not_decided_again_on_that_bar(
        self, outcome, restored
    ):
        """The same targets on the same bar and book would send the failed leg again."""
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa(outcome)
        strategy = _SameTargets({"BBB": 0.0})
        trader, events, _ = _trader(adapter, strategy=strategy)
        bar_times = _continue_on_the_next_bar(trader, strategy.weights)
        if restored:
            trader, events, _ = _trader(adapter, strategy=strategy, state_store=trader._state_store)
            assert trader._live_rebalance is not None
        trader._positions = {
            symbol: replace(_position(symbol), group_id="pair") for symbol in ("AAA", "BBB")
        }

        _next_bar(trader, bar_times)

        assert _submitted(adapter) == ["AAA"]
        assert strategy.calls == 0
        assert trader._live_rebalance is None
        assert trader._halted is False

        _next_bar(trader, bar_times)

        assert strategy.calls == 1
        assert _submitted(adapter) == ["AAA", "AAA"], "the next bar's decision sends it once"
        assert len(_of_type(events, "group_failed")) == 2

    def test_a_bar_retried_after_the_stop_is_not_decided_on_either(self):
        """A checkpoint failure after the stop leaves the bar for the next poll to retry."""
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = _refusing_aaa("rejected")
        strategy = _SameTargets({"BBB": 0.0})
        trader, events, _ = _trader(adapter, strategy=strategy)
        bar_times = _continue_on_the_next_bar(trader, strategy.weights)
        trader._positions = {
            symbol: replace(_position(symbol), group_id="pair") for symbol in ("AAA", "BBB")
        }
        persist = trader._persist_state
        failed: list[bool] = []

        def persist_failing_once_the_bar_executed(*orders):
            if not failed and trader._last_execution_bar_ts.get("AAA") == bar_times[-1]:
                failed.append(True)
                raise OSError(OUTAGE)
            persist(*orders)

        trader._persist_state = persist_failing_once_the_bar_executed
        with pytest.raises(OSError, match=OUTAGE):
            _next_bar(trader, bar_times)
        trader._process_cycle(
            {symbol: _make_ohlcv_at(bar_times) for symbol in SYMBOLS}, bar_times[-1]
        )

        assert failed == [True]
        assert _submitted(adapter) == ["AAA"]
        assert strategy.calls == 0
        assert len(_of_type(events, "group_failed")) == 1

        _next_bar(trader, bar_times)

        assert strategy.calls == 1
        assert _submitted(adapter) == ["AAA", "AAA"]

    def test_a_rebalance_an_unfunded_entry_stopped_is_not_decided_again_on_that_bar(self):
        adapter = _adapter(below_minimum="")
        adapter.get_balance.return_value = {"free": 0.0, "used": 0.0, "total": 0.0}
        strategy = _SameTargets({"AAA": 0.5})
        trader, events, _ = _trader(adapter, strategy=strategy)
        trader._positions = {}
        bar_times = _continue_on_the_next_bar(trader, strategy.weights)

        _next_bar(trader, bar_times)

        adapter.place_order.assert_not_called()
        assert strategy.calls == 0
        assert trader._live_rebalance is None
        assert len(_of_type(events, "decision_skipped")) == 1

        _next_bar(trader, bar_times)

        assert strategy.calls == 1
        assert len(_of_type(events, "decision_skipped")) == 2

    @pytest.mark.parametrize("grouped", [True, False])
    @pytest.mark.parametrize("ends_after", ["restart", "pending_cancel"])
    def test_a_rebalance_leg_whose_cancel_ends_later_fails_as_one_ending_at_once(
        self, ends_after, grouped
    ):
        """A cancel finished in a later cycle must not let the rebalance send the leg again.

        After a restart, the timeout's cancel ends there as Order Timeout; a
        cancel the venue only acknowledged ends as Order Cancelled.
        """
        title = "Order Timeout" if ends_after == "restart" else "Order Cancelled"
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        adapter = _resting_adapter()
        now = [TEST_CLOCK_NOW]
        trader, events, alerts = _timeout_trader(adapter, store, now)
        trader._positions = {
            symbol: replace(_position(symbol), group_id="pair" if grouped else None)
            for symbol in ("AAA", "BBB")
        }
        trader._execute_live_decision(PortfolioWeights({"BBB": 0.0}), _bars(), TEST_CLOCK_NOW)
        assert [order.request.symbol for order in trader._active_orders] == ["AAA"]

        if ends_after == "restart":
            # The timeout's cancel is checkpointed, then the process dies sending it.
            now[0] += timedelta(seconds=30)
            adapter.cancel_order.side_effect = _Crash
            with pytest.raises(_Crash):
                trader._advance_live_orders()
            adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")
            adapter.get_position.side_effect = lambda request: _held(request, "AAA", "BBB")
            trader, events, alerts = _timeout_trader(adapter, store, now)
            trader._initialize_run()
        else:
            answers = iter(["pending_cancel", "canceled"])
            adapter.get_order.side_effect = lambda order_id, _s: _resting(order_id, next(answers))
            trader._advance_live_orders()
            assert trader._active_orders[0].status == "cancel_pending"
            trader._advance_live_orders()
        # The next bar.
        trader._continue_live_rebalance(_bars(), now[0])

        assert _submitted(adapter) == ["AAA"]
        assert trader._active_orders == []
        assert trader._live_rebalance is None
        assert trader._state_store.load(trader._state_key).live_rebalance is None
        if grouped:
            assert trader._halted is False
            [failed] = _of_type(events, "group_failed")
            assert failed.detail["group_id"] == "pair"
            [alert] = _titled(alerts, title)
            assert "target rebalance stopped" in alert["message"]
        else:
            assert trader._halted is True
            [halted] = _of_type(events, "halted")
            assert halted.detail["reason"] == title

    def test_a_flatten_cancel_finished_after_a_restart_fails_nothing(self):
        """Only an over-age order fails when an engine cancel ends after a restart."""
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        adapter = _resting_adapter()
        now = [TEST_CLOCK_NOW]
        trader, _, _ = _timeout_trader(adapter, store, now)
        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )
        trader.request_flatten("desk asked to close")
        adapter.cancel_order.side_effect = _Crash
        with pytest.raises(_Crash):
            trader._poll_cycle()
        adapter.cancel_order.side_effect = lambda order_id, _s: _resting(order_id, "canceled")

        trader, events, alerts = _timeout_trader(adapter, store, now)
        trader._initialize_run()

        assert trader._active_orders == []
        assert trader._halted is False
        assert [event.event_type for event in events] == ["state_recovered"]
        assert alerts == []


class _Crash(BaseException):
    """The process dies inside a broker call, past every ``except Exception``."""


def _timeout_trader(adapter, store, now: list[datetime]):
    """A trader on ``store`` whose orders time out 30 seconds after placement."""
    events: list = []
    alerts: list[dict] = []
    trader = make_live_trader(
        _HoldStrategy(),
        adapter,
        config=_test_cfg(
            mode="live",
            symbols=SYMBOLS,
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                warmup_periods=5,
                live_order_timeout_seconds=30,
            ),
        ),
        state_store=store,
        clock=lambda: now[0],
        on_runtime_event=events.append,
    )
    trader._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
    trader._last_prices = {symbol: 100.0 for symbol in SYMBOLS}
    return with_run_leases(trader), events, alerts


def _held(request, *symbols: str) -> dict:
    """The broker's answer for a book holding one long of each of ``symbols``."""
    size = 1.0 if request.symbol in symbols else 0.0
    return {"symbol": request.symbol, "size": size, "avg_price": 100.0, "unrealized_pnl": 0.0}


class TestPlacementErrorReachesTheOrderRow:
    def test_a_definite_refusal_is_rejected_with_the_venue_reason(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = OrderRejectedError("ReduceOnly Order is rejected.")
        trader = make_live_trader(_HoldStrategy(), adapter)
        trader._last_prices = {"BTCUSDT": 100.0}

        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0)],
            {"BTCUSDT": {"close": 100.0, "volume": 10_000.0}},
            TEST_CLOCK_NOW,
        )

        [row] = trader._state_store.orders.values()
        assert row.status == "rejected"
        assert row.error == "ReduceOnly Order is rejected."

    def test_an_unknown_outcome_keeps_its_status_and_records_the_error(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = TimeoutError("read timed out")
        trader = make_live_trader(_HoldStrategy(), adapter)
        trader._last_prices = {"BTCUSDT": 100.0}

        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0)],
            {"BTCUSDT": {"close": 100.0, "volume": 10_000.0}},
            TEST_CLOCK_NOW,
        )

        assert trader._halted is True
        [row] = trader._state_store.orders.values()
        # It may rest at the venue: rejected would stop anything from looking for it.
        assert row.status == "submitted"
        assert row.placement_attempted is True
        assert row.error == "TimeoutError: read timed out"
        assert [order.error for order in trader._active_orders] == [row.error]

    def test_a_store_outage_after_a_failed_lookup_still_halts_on_the_next_lookup(self):
        """A store that is down cannot fail the cycle before the lookup that
        halts the account."""
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = TimeoutError("read timed out")
        adapter.find_order.side_effect = ConnectionError("lookup failed")
        trader, _, alerts = _live_trader(adapter, "BTCUSDT")
        with pytest.raises(ConnectionError, match="lookup failed"):
            _buy_btc(trader)
        assert trader._halted is False
        _store_outage(trader)
        adapter.find_order.side_effect = None
        adapter.find_order.return_value = None

        with pytest.raises(OSError, match=OUTAGE):
            trader._poll_cycle()

        assert trader._halted is True
        [halt] = _titled(alerts, "Ambiguous Restored Order")
        assert "not persisted" in halt["message"]

    def test_an_unknown_outcome_whose_lookup_fails_still_records_the_error(self):
        """The checkpoint leaves the error out, so it must not wait for a later
        write that a restart would never make."""
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = TimeoutError("read timed out")
        adapter.find_order.side_effect = ConnectionError("lookup failed")
        trader, _, _ = _live_trader(adapter, "BTCUSDT")

        with pytest.raises(ConnectionError, match="lookup failed"):
            _buy_btc(trader)

        [row] = trader._state_store.orders.values()
        assert row.status == "submitted"
        assert row.error == "TimeoutError: read timed out"

    def test_an_error_write_the_store_fails_still_halts_on_the_lookup(self):
        adapter = _mock_order_adapter()
        adapter.find_order.return_value = None
        trader, _, alerts = _live_trader(adapter, "BTCUSDT")
        outage = _store_outage(trader)
        outage["down"] = False

        def place_order(_request):
            outage["down"] = True
            raise TimeoutError("read timed out")

        adapter.place_order.side_effect = place_order

        with pytest.raises(OSError, match=OUTAGE):
            _buy_btc(trader)

        adapter.find_order.assert_called_once()
        assert trader._halted is True
        assert _titled(alerts, "Ambiguous Order Placement")
        outage["down"] = False
        trader._poll_cycle()
        [row] = trader._state_store.orders.values()
        assert row.error == "TimeoutError: read timed out"

    def test_an_unknown_outcome_found_by_lookup_keeps_the_error(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = TimeoutError("read timed out")
        adapter.find_order.return_value = _broker_report(
            order_id="broker-1", status="open", quantity=1.0, filled=0.0
        )
        trader = make_live_trader(_HoldStrategy(), adapter)
        trader._last_prices = {"BTCUSDT": 100.0}

        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0)],
            {"BTCUSDT": {"close": 100.0, "volume": 10_000.0}},
            TEST_CLOCK_NOW,
        )

        assert trader._halted is False
        [row] = trader._state_store.orders.values()
        assert row.status == "accepted"
        assert row.order_id == "broker-1"
        assert row.error == "TimeoutError: read timed out"
