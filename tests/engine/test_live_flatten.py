"""Closing the account must reach every position the venue can still take.

An ungrouped reduce/close that the adapter rejects as below the venue minimum
is skipped for that symbol, recorded, and alerted once, while the remaining
exits still go out. Entries and every other preparation error stay fail-closed.
A drawdown or flatten exit that ends rejected, cancelled or timed out is
skipped the same way, while a close that only borrows a recovery reason on a
running account keeps halting. An operator flatten runs the drawdown path's
close-everything-and-halt on the polling thread, so the caller's thread never
touches engine state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Lock, Thread, get_ident
from unittest.mock import MagicMock

import pytest
from librae.brokers.crypto_adapter import CryptoAdapter, _require_ccxt
from librae.core.executor import REASON_OPERATOR_FLATTEN
from librae.core.run_config import RiskPolicy
from librae.core.strategy import OrderIntent, PortfolioWeights, PositionState, Strategy
from librae.live.executor import OrderBelowVenueMinimumError, OrderRejectedError, OrderRequest
from librae.live.state import LiveRebalance, TrackedOrder

from tests.engine import test_live_runner
from tests.engine.test_live_runner import (
    TEST_CLOCK_NOW,
    _broker_report,
    _HoldStrategy,
    _mock_order_adapter,
    _test_cfg,
)

SYMBOLS = ["AAA", "BBB", "DUST"]


def _position(symbol: str, quantity: float = 1.0) -> PositionState:
    return PositionState(
        symbol=symbol,
        side="long",
        entry_price=100.0,
        quantity=quantity,
        entry_at=datetime(2025, 1, 1, tzinfo=UTC),
        periods_held=1,
        entry_commission=0.0,
        entry_slippage=0.0,
        entry_tax=0.0,
        total_entry_cost=100.0 * quantity,
    )


def _adapter(below_minimum: str = "DUST"):
    adapter = _mock_order_adapter()

    def prepare_order(signal):
        if signal["canonical_symbol"] == below_minimum:
            raise OrderBelowVenueMinimumError(f"{below_minimum} notional 1.0 is below minimum 5")
        return signal

    adapter.prepare_order.side_effect = prepare_order
    adapter.place_order.side_effect = lambda signal: _broker_report(
        order_id=signal["client_order_id"], quantity=signal["quantity"]
    )
    return adapter


def _trader(adapter, strategy: Strategy | None = None, **kwargs):
    events: list = []
    alerts: list[dict] = []
    # Importing the class itself would make pytest collect its tests here too.
    trader = test_live_runner.TestLiveExecutionLifecycle()._make_trader(
        strategy or _HoldStrategy(),
        adapter,
        config=_test_cfg(mode="live", symbols=SYMBOLS),
        on_runtime_event=events.append,
        **kwargs,
    )
    trader._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
    trader._positions = {symbol: _position(symbol) for symbol in SYMBOLS}
    trader._last_prices = {symbol: 100.0 for symbol in SYMBOLS}
    return trader, events, alerts


def _crypto_adapter(limits: dict | None = None, *, zero_amount: str = ""):
    """Order adapter whose preparation is the real CryptoAdapter's.

    ``zero_amount`` names a symbol whose amount ccxt refuses as rounding to zero.
    """
    exchange = MagicMock()
    exchange.market.side_effect = lambda symbol: {
        "symbol": symbol,
        "type": "spot",
        "spot": True,
        "limits": limits or {},
    }

    def amount_to_precision(symbol, amount):
        if symbol == zero_amount:
            raise _require_ccxt().InvalidOrder(
                f"binance amount of {symbol} must be greater than minimum amount precision"
            )
        return str(amount)

    exchange.amount_to_precision.side_effect = amount_to_precision
    exchange.price_to_precision.side_effect = lambda _symbol, price: str(price)
    crypto = CryptoAdapter.__new__(CryptoAdapter)
    crypto._exchange = exchange
    crypto._read_only = False
    crypto._exchange_id = "binance"
    adapter = _adapter(below_minimum="")
    adapter.prepare_order.side_effect = crypto.prepare_order
    return adapter


def _submitted(adapter) -> list[str]:
    return [call.args[0]["canonical_symbol"] for call in adapter.place_order.call_args_list]


def _dust_skips(events) -> list:
    return [
        event
        for event in events
        if event.event_type == "decision_skipped"
        and event.detail.get("reason") == "close_below_venue_minimum"
    ]


def _titled(alerts, text: str) -> list[dict]:
    return [alert for alert in alerts if text in alert["title"]]


def _close_all() -> list[OrderIntent]:
    return [OrderIntent(action="close", symbol=symbol) for symbol in SYMBOLS]


def _bars() -> dict[str, dict[str, float]]:
    return {symbol: {"close": 100.0, "volume": 10_000.0} for symbol in SYMBOLS}


class TestBelowMinimumClose:
    def test_drawdown_flatten_skips_the_dust_close_and_submits_the_rest(self):
        adapter = _adapter()
        trader, events, alerts = _trader(adapter)
        trader._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
        trader._cash = 0.0
        trader._equity_peak = 1_000.0

        trader._record_equity(TEST_CLOCK_NOW)

        assert _submitted(adapter) == ["AAA", "BBB"]
        assert set(trader._positions) == {"DUST"}
        assert trader._halted is True
        [skip] = _dust_skips(events)
        assert skip.symbol == "DUST"
        assert skip.detail["quantity"] == pytest.approx(1.0)
        assert "below minimum 5" in skip.detail["message"]
        assert len(_titled(alerts, "Close Below Venue Minimum")) == 1
        [breach] = _titled(alerts, "Max Drawdown Breach")
        assert "flatten attempt failed" not in breach["message"]
        assert "DUST" in breach["message"]

    def test_flatten_skips_a_remainder_ccxt_rounds_to_zero(self):
        adapter = _crypto_adapter(zero_amount="DUST")
        trader, events, _ = _trader(adapter)
        trader._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
        trader._cash = 0.0
        trader._equity_peak = 1_000.0

        trader._record_equity(TEST_CLOCK_NOW)

        assert _submitted(adapter) == ["AAA", "BBB"]
        assert set(trader._positions) == {"DUST"}
        assert trader._halted is True
        [skip] = _dust_skips(events)
        assert "rounds to zero" in skip.detail["message"]

    def test_close_limit_below_the_price_band_still_halts(self):
        adapter = _crypto_adapter({"price": {"min": 10.0, "max": None}})
        trader, events, alerts = _trader(adapter)

        complete = trader._execute_live_decision(
            [OrderIntent(action="close", symbol="AAA", limit_price=5.0)], _bars(), TEST_CLOCK_NOW
        )

        assert complete is False
        assert trader._halted is True
        assert _titled(alerts, "Live Order Preflight Rejected")
        assert _dust_skips(events) == []
        adapter.place_order.assert_not_called()

    def test_reset_halt_forgets_which_remainders_were_alerted(self):
        adapter = _adapter()
        trader, _, alerts = _trader(adapter)
        trader._positions = {"DUST": _position("DUST", 0.01)}
        close_dust = [OrderIntent(action="close", symbol="DUST")]

        trader._execute_live_decision(close_dust, _bars(), TEST_CLOCK_NOW)
        trader._halted = True
        trader._last_bar_ts = {"DUST": TEST_CLOCK_NOW}
        trader.reset_halt()
        trader._execute_live_decision(close_dust, _bars(), TEST_CLOCK_NOW)

        assert len(_titled(alerts, "Close Below Venue Minimum")) == 2

    def test_repeated_dust_close_alerts_once_per_position_quantity(self):
        adapter = _adapter()
        trader, events, alerts = _trader(adapter)
        trader._positions = {"DUST": _position("DUST", 0.01)}

        for _ in range(3):
            assert trader._execute_live_decision(
                [OrderIntent(action="close", symbol="DUST")], _bars(), TEST_CLOCK_NOW
            )
        trader._positions["DUST"].quantity = 0.02
        trader._execute_live_decision(
            [OrderIntent(action="close", symbol="DUST")], _bars(), TEST_CLOCK_NOW
        )

        assert trader._halted is False
        assert len(_dust_skips(events)) == 4
        assert len(_titled(alerts, "Close Below Venue Minimum")) == 2
        adapter.place_order.assert_not_called()

    def test_skipped_close_leaves_the_planned_book_unchanged(self):
        adapter = _adapter()

        def prepare_order(signal):
            if signal["position_effect"] == "close":
                raise OrderBelowVenueMinimumError("DUST notional 1.0 is below minimum 5")
            return signal

        adapter.prepare_order.side_effect = prepare_order
        trader, _, _ = _trader(adapter)
        trader._positions = {"DUST": _position("DUST")}
        trader._cash = 1_000.0

        trader._execute_live_decision(
            [
                OrderIntent(action="close", symbol="DUST"),
                OrderIntent(action="long", symbol="DUST", quantity=1.0),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        [order] = [call.args[0] for call in adapter.place_order.call_args_list]
        assert order["position_effect"] == "add"
        assert trader._positions["DUST"].quantity == pytest.approx(2.0)

    def test_skipped_reduce_leaves_the_planned_book_unchanged(self):
        adapter = _adapter()

        def prepare_order(signal):
            if signal["position_effect"] == "reduce":
                raise OrderBelowVenueMinimumError("DUST notional 50.0 is below minimum 60")
            return signal

        adapter.prepare_order.side_effect = prepare_order
        trader, events, _ = _trader(adapter)
        trader._positions = {"DUST": _position("DUST")}

        trader._execute_live_decision(
            [
                OrderIntent(action="close", symbol="DUST", quantity=0.5),
                OrderIntent(action="close", symbol="DUST"),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        [order] = [call.args[0] for call in adapter.place_order.call_args_list]
        assert order["position_effect"] == "close"
        assert order["quantity"] == pytest.approx(1.0)
        assert len(_dust_skips(events)) == 1

    def test_entry_below_minimum_still_halts(self):
        adapter = _adapter()
        trader, events, alerts = _trader(adapter)
        trader._positions = {}

        complete = trader._execute_live_decision(
            [OrderIntent(action="long", symbol="DUST", quantity=0.01)], _bars(), TEST_CLOCK_NOW
        )

        assert complete is False
        assert trader._halted is True
        assert _titled(alerts, "Live Order Preflight Rejected")
        assert _dust_skips(events) == []
        adapter.place_order.assert_not_called()

    def test_other_preparation_error_on_a_close_still_halts(self):
        adapter = _mock_order_adapter()

        def prepare_order(signal):
            if signal["canonical_symbol"] == "DUST":
                raise ValueError("DUST quantity 1.0 exceeds maximum 0.5")
            return signal

        adapter.prepare_order.side_effect = prepare_order
        trader, events, alerts = _trader(adapter)

        complete = trader._execute_live_decision(_close_all(), _bars(), TEST_CLOCK_NOW)

        assert complete is False
        assert trader._halted is True
        assert _titled(alerts, "Live Order Preflight Rejected")
        assert _dust_skips(events) == []
        adapter.place_order.assert_not_called()

    def test_grouped_close_below_minimum_keeps_group_semantics(self):
        adapter = _adapter()
        trader, events, _ = _trader(adapter)

        trader._execute_live_decision(
            [
                OrderIntent(action="close", symbol="AAA", group_id="pair"),
                OrderIntent(action="close", symbol="DUST", group_id="pair"),
                OrderIntent(action="close", symbol="BBB"),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        assert _submitted(adapter) == ["BBB"]
        assert set(trader._positions) == {"AAA", "DUST"}
        assert trader._halted is False
        assert _dust_skips(events) == []
        assert [event.detail["reason"] for event in events] == ["group_preflight_rejected"]


class _CountingStrategy(Strategy):
    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, ctx):
        self.calls += 1
        return []


def _accepted(order_id: str) -> dict:
    return {"id": order_id, "status": "open", "amount": 1.0, "filled": 0.0}


class TestOperatorFlatten:
    def test_request_from_another_thread_only_records_it(self):
        adapter = _adapter(below_minimum="")
        trader, events, alerts = _trader(adapter)
        persisted: list = []
        trader._persist_state = lambda *orders: persisted.append(orders)

        caller = Thread(target=trader.request_flatten, args=("desk asked to stop",))
        caller.start()
        caller.join()

        assert trader._halted is False
        assert set(trader._positions) == set(SYMBOLS)
        adapter.prepare_order.assert_not_called()
        adapter.place_order.assert_not_called()
        assert persisted == []
        assert events == []
        assert alerts == []

    def test_request_rejects_an_empty_reason(self):
        trader, _, _ = _trader(_adapter())

        with pytest.raises(ValueError, match="reason"):
            trader.request_flatten("  ")

    def test_next_cycle_flattens_and_halts_before_strategy_evaluation(self):
        adapter = _adapter()
        submitting_threads: list[int] = []

        def place_order(signal):
            submitting_threads.append(get_ident())
            return _broker_report(order_id=signal["client_order_id"], quantity=signal["quantity"])

        adapter.place_order.side_effect = place_order
        strategy = _CountingStrategy()
        trader, events, alerts = _trader(adapter, strategy=strategy)

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()
        trader._poll_cycle()

        assert _submitted(adapter) == ["AAA", "BBB"]
        assert submitting_threads == [get_ident(), get_ident()]
        assert set(trader._positions) == {"DUST"}
        assert trader._halted is True
        assert strategy.calls == 0
        assert len(_dust_skips(events)) == 1
        [flatten] = _titled(alerts, "Operator Flatten")
        assert "desk asked to stop" in flatten["message"]
        assert "DUST" in flatten["message"]

    def test_deferred_rebalance_does_not_survive_the_flatten(self):
        trader, _, _ = _trader(_adapter(below_minimum=""))
        trader._live_rebalance = LiveRebalance(
            targets=PortfolioWeights({"AAA": 0.5}),
            reference_prices={"AAA": 100.0},
            reference_volumes={"AAA": 1_000.0},
            lagged_adv_by_symbol={},
            decided_at=TEST_CLOCK_NOW,
            delay_bars=1,
        )

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        assert trader._halted is True
        assert trader._live_rebalance is None

    def test_position_without_a_mark_stays_open_and_is_recorded(self):
        adapter = _adapter(below_minimum="")
        trader, events, alerts = _trader(adapter)
        del trader._last_prices["DUST"]

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        assert _submitted(adapter) == ["AAA", "BBB"]
        assert set(trader._positions) == {"DUST"}
        assert trader._halted is True
        [skip] = [
            event
            for event in events
            if event.event_type == "decision_skipped"
            and event.detail.get("reason") == "close_without_mark"
        ]
        assert skip.symbol == "DUST"
        assert skip.detail["quantity"] == pytest.approx(1.0)
        assert len(_titled(alerts, "Close Without Mark: DUST")) == 1
        [flatten] = [alert for alert in alerts if alert["title"].endswith("Operator Flatten")]
        assert "DUST" in flatten["message"]

    def test_each_skip_reason_alerts_on_its_own(self):
        adapter = _adapter()
        trader, _, alerts = _trader(adapter)
        trader._positions = {"DUST": _position("DUST")}

        trader._execute_live_decision(
            [OrderIntent(action="close", symbol="DUST")], _bars(), TEST_CLOCK_NOW
        )
        del trader._last_prices["DUST"]
        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        assert len(_titled(alerts, "Close Below Venue Minimum: DUST")) == 1
        assert len(_titled(alerts, "Close Without Mark: DUST")) == 1

    def test_request_arriving_while_one_is_handled_is_kept(self):
        adapter = _adapter(below_minimum="")
        trader, _, alerts = _trader(adapter)
        trader._halted = True

        class InterleavingLock:
            """Lets a second request land between the loop's read and its clear."""

            def __init__(self) -> None:
                self._lock = Lock()
                self.entries = 0

            def __enter__(self):
                self.entries += 1
                if self.entries == 3:
                    trader.request_flatten("second request")
                return self._lock.__enter__()

            def __exit__(self, *exc):
                return self._lock.__exit__(*exc)

        trader._flatten_request_lock = InterleavingLock()
        trader.request_flatten("first request")
        trader._poll_cycle()
        trader._halted = False
        trader._poll_cycle()

        [refused] = _titled(alerts, "Operator Flatten Refused")
        assert "first request" in refused["message"]
        [flatten] = [alert for alert in alerts if alert["title"].endswith("Operator Flatten")]
        assert "second request" in flatten["message"]
        assert trader._positions == {}

    def test_request_after_a_reset_waits_for_a_matching_reconciliation(self):
        adapter = _adapter(below_minimum="")
        adapter.get_position.side_effect = lambda request: {
            "symbol": request.symbol,
            "size": 1.0,
            "avg_price": 100.0,
            "unrealized_pnl": 0.0,
        }
        trader, _, _ = _trader(adapter)
        trader._halted = True
        trader._last_bar_ts = {symbol: TEST_CLOCK_NOW for symbol in SYMBOLS}
        trader.reset_halt()

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        adapter.place_order.assert_not_called()
        assert trader._halted is False

        trader._poll_cycle()

        assert _submitted(adapter) == SYMBOLS
        assert trader._halted is True

    def test_halted_account_refuses_the_request(self):
        adapter = _adapter(below_minimum="")
        trader, _, alerts = _trader(adapter)
        trader._halted = True

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()
        trader._poll_cycle()

        adapter.place_order.assert_not_called()
        assert set(trader._positions) == set(SYMBOLS)
        assert len(_titled(alerts, "Operator Flatten Refused")) == 1

    def test_resting_order_is_cancelled_before_the_exits(self):
        adapter = _adapter(below_minimum="")
        adapter.get_order.return_value = _accepted("rest-1")
        adapter.cancel_order.return_value = {
            "id": "rest-1",
            "status": "pendingcancel",
            "amount": 1.0,
            "filled": 0.0,
        }
        strategy = _CountingStrategy()
        trader, _, _ = _trader(adapter, strategy=strategy)
        trader._active_orders = [
            TrackedOrder(
                request=OrderRequest(
                    client_order_id="rest-1",
                    symbol="AAA",
                    side="buy",
                    quantity=1.0,
                    order_type="limit",
                    limit_price=90.0,
                    submitted_at=TEST_CLOCK_NOW,
                ),
                placement_attempted=True,
                placement_attempted_at=TEST_CLOCK_NOW,
                order_id="rest-1",
                status="accepted",
            )
        ]

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        adapter.cancel_order.assert_called_once()
        adapter.place_order.assert_not_called()
        assert trader._halted is False
        assert strategy.calls == 0

        adapter.get_order.return_value = {
            "id": "rest-1",
            "status": "canceled",
            "amount": 1.0,
            "filled": 0.0,
        }
        trader._poll_cycle()

        assert _submitted(adapter) == SYMBOLS
        assert trader._positions == {}
        assert trader._halted is True
        assert strategy.calls == 0

    def test_resting_exit_keeps_advancing_while_halted(self):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = lambda signal: _accepted(signal["client_order_id"])
        trader, _, _ = _trader(adapter)
        trader._positions = {"AAA": _position("AAA")}

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        assert trader._halted is True
        [exit_order] = trader._active_orders
        assert exit_order.request.reason == REASON_OPERATOR_FLATTEN

        adapter.get_order.return_value = _broker_report(
            order_id=exit_order.request.client_order_id, quantity=1.0
        )
        trader._poll_cycle()

        adapter.cancel_order.assert_not_called()
        assert trader._active_orders == []
        assert trader._positions == {}

    def test_sim_exits_at_the_next_observed_open_and_halts(self):
        strategy = _CountingStrategy()
        runner = test_live_runner.TestLiveTrader()._make_runner(
            strategy=strategy,
            config=_test_cfg(mode="sim"),
        )
        position_events: list = []
        runner._on_position_event = lambda event, _sequence: position_events.append(event)
        runner._positions = {"BTCUSDT": _position("BTCUSDT")}
        runner._last_prices = {"BTCUSDT": 100.0}

        runner.request_flatten("desk asked to stop")
        runner._poll_cycle()

        assert runner._halted is True
        assert runner._positions == {}
        assert [(event.event_type, event.reason) for event in position_events] == [
            ("close", REASON_OPERATOR_FLATTEN)
        ]
        assert strategy.calls == 0


_REDUCE_ONLY_REFUSAL = 'binance {"code":-2022,"msg":"ReduceOnly Order is rejected."}'


def _refusing(symbol: str, error: OrderRejectedError, *, working: bool):
    """Adapter that refuses ``symbol``'s exit and accepts or fills the rest."""
    adapter = _adapter(below_minimum="")

    def place_order(signal):
        if signal["canonical_symbol"] == symbol:
            raise error
        order_id = signal["client_order_id"]
        if working:
            return _accepted(order_id)
        return _broker_report(order_id=order_id, quantity=signal["quantity"])

    adapter.place_order.side_effect = place_order
    adapter.get_order.side_effect = lambda order_id, _symbol: _broker_report(
        order_id=order_id, quantity=1.0
    )
    return adapter


def _skips(events, reason: str) -> list:
    return [
        event
        for event in events
        if event.event_type == "decision_skipped" and event.detail.get("reason") == reason
    ]


def _refusal_skips(events) -> list:
    return _skips(events, "close_rejected")


class TestRefusedRecoveryExit:
    def test_refused_flatten_exit_does_not_strand_the_others(self):
        adapter = _refusing("BBB", OrderRejectedError(_REDUCE_ONLY_REFUSAL), working=True)
        trader, events, alerts = _trader(adapter)

        trader.request_flatten("desk asked to stop")
        for _ in range(3):
            trader._poll_cycle()

        assert _submitted(adapter) == SYMBOLS
        adapter.cancel_order.assert_not_called()
        adapter.find_order.assert_not_called()
        assert trader._active_orders == []
        assert set(trader._positions) == {"BBB"}
        assert trader._halted is True
        assert trader.halt_reset_readiness().reason != "unresolved_broker_orders"
        [skip] = _refusal_skips(events)
        assert skip.symbol == "BBB"
        assert "ReduceOnly Order is rejected." in skip.detail["message"]
        [alert] = _titled(alerts, "Close Rejected: BBB")
        assert "ReduceOnly Order is rejected." in alert["message"]
        assert _titled(alerts, "Order Rejected") == []

    def test_refused_drawdown_exit_does_not_strand_the_others(self):
        adapter = _refusing("AAA", OrderRejectedError(_REDUCE_ONLY_REFUSAL), working=False)
        trader, events, alerts = _trader(adapter)
        trader._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
        trader._cash = 0.0
        trader._equity_peak = 1_000.0

        bar_ts = datetime(2025, 1, 1, 9, tzinfo=UTC)

        trader._record_equity(bar_ts)

        assert _submitted(adapter) == SYMBOLS
        assert set(trader._positions) == {"AAA"}
        assert trader._halted is True
        [skip] = _refusal_skips(events)
        assert (skip.symbol, skip.ts) == ("AAA", bar_ts)
        [breach] = _titled(alerts, "Max Drawdown Breach")
        assert "closed all but AAA" in breach["message"]

    def test_account_fault_refusal_during_a_flatten_keeps_the_other_exits(self):
        adapter = _refusing(
            "BBB",
            OrderRejectedError("Timestamp outside of the recvWindow", account_fault=True),
            working=True,
        )
        trader, _, alerts = _trader(adapter)

        trader.request_flatten("desk asked to stop")
        for _ in range(3):
            trader._poll_cycle()

        assert _submitted(adapter) == SYMBOLS
        adapter.cancel_order.assert_not_called()
        assert trader._active_orders == []
        assert set(trader._positions) == {"BBB"}
        assert trader._halted is True
        [alert] = _titled(alerts, "Close Rejected: BBB")
        assert "recvWindow" in alert["message"]
        assert "refused the account" in alert["message"]
        assert _titled(alerts, "Order Rejected") == []

    def test_cancelled_flatten_exits_are_recorded_and_the_rest_submitted(self):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = lambda signal: {
            "id": signal["client_order_id"],
            "status": "canceled",
            "amount": signal["quantity"],
            "filled": 0.0,
        }
        trader, events, alerts = _trader(adapter)

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        assert _submitted(adapter) == SYMBOLS
        assert set(trader._positions) == set(SYMBOLS)
        assert trader._halted is True
        assert [event.symbol for event in _skips(events, "close_cancelled")] == SYMBOLS
        assert len(_titled(alerts, "Close Cancelled")) == 3
        assert _titled(alerts, "Order Cancelled") == []
        [flatten] = [alert for alert in alerts if alert["title"].endswith("Operator Flatten")]
        assert "no position fully closed; AAA, BBB, DUST remain open" in flatten["message"]

    def test_timed_out_flatten_exit_is_recorded_and_the_rest_submitted(self):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = lambda signal: (
            _accepted(signal["client_order_id"])
            if signal["canonical_symbol"] == "BBB"
            else _broker_report(order_id=signal["client_order_id"], quantity=signal["quantity"])
        )
        adapter.cancel_order.side_effect = lambda order_id, _symbol: {
            "id": order_id,
            "status": "canceled",
            "amount": 1.0,
            "filled": 0.0,
        }
        trader, events, _ = _trader(adapter)
        trader._live_order_timeout_seconds = 0.0

        trader.request_flatten("desk asked to stop")
        for _ in range(2):
            trader._poll_cycle()

        assert _submitted(adapter) == SYMBOLS
        adapter.cancel_order.assert_called_once()
        assert trader._active_orders == []
        assert set(trader._positions) == {"BBB"}
        assert trader._halted is True
        [skip] = _skips(events, "close_cancelled")
        assert skip.symbol == "BBB"
        assert "Order Timeout" in skip.detail["message"]

    def test_every_refused_exit_reports_that_no_position_fully_closed(self):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = OrderRejectedError(_REDUCE_ONLY_REFUSAL)
        trader, events, alerts = _trader(adapter)

        trader.request_flatten("desk asked to stop")
        trader._poll_cycle()

        assert _submitted(adapter) == SYMBOLS
        assert len(_refusal_skips(events)) == 3
        [flatten] = [alert for alert in alerts if alert["title"].endswith("Operator Flatten")]
        assert "no position fully closed; AAA, BBB, DUST remain open" in flatten["message"]
        assert "closed all but" not in flatten["message"]

    def test_close_with_a_spoofed_recovery_reason_on_a_running_account_halts(self):
        adapter = _refusing("AAA", OrderRejectedError(_REDUCE_ONLY_REFUSAL), working=False)
        trader, events, alerts = _trader(adapter)

        complete = trader._execute_live_decision(
            [
                OrderIntent(action="close", symbol=symbol, reason=REASON_OPERATOR_FLATTEN)
                for symbol in SYMBOLS
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        assert complete is False
        assert _submitted(adapter) == ["AAA"]
        assert trader._halted is True
        assert _refusal_skips(events) == []
        assert _titled(alerts, "Order Rejected")

    def test_failed_entry_with_a_recovery_reason_halts_even_while_halted(self):
        trader, events, _ = _trader(_adapter(below_minimum=""))
        trader._halted = True
        tracked = TrackedOrder(
            request=OrderRequest(
                client_order_id="entry-1",
                symbol="AAA",
                side="buy",
                quantity=1.0,
                order_type="market",
                submitted_at=TEST_CLOCK_NOW,
                reason=REASON_OPERATOR_FLATTEN,
                position_effect="open",
            ),
            status="rejected",
        )

        assert trader._fail_group_or_halt(tracked, title="Order Rejected", message="AAA") is True
        assert _refusal_skips(events) == []

    def test_refused_strategy_close_still_halts(self):
        adapter = _refusing("AAA", OrderRejectedError(_REDUCE_ONLY_REFUSAL), working=False)
        trader, events, alerts = _trader(adapter)

        complete = trader._execute_live_decision(_close_all(), _bars(), TEST_CLOCK_NOW)

        assert complete is False
        assert _submitted(adapter) == ["AAA"]
        assert trader._halted is True
        assert _refusal_skips(events) == []
        [alert] = _titled(alerts, "Order Rejected")
        assert "ReduceOnly Order is rejected." in alert["message"]

    def test_skipped_close_names_a_position_gone_from_the_ledger(self):
        trader, _, alerts = _trader(_adapter(below_minimum=""))

        trader._report_skipped_close(
            "AAA",
            quantity=1.0,
            held=None,
            ts=TEST_CLOCK_NOW,
            reason="close_rejected",
            title="Close Rejected",
            cause="venue refused",
        )

        [alert] = _titled(alerts, "Close Rejected: AAA")
        assert "no longer in the ledger" in alert["message"]
        assert "stays open" not in alert["message"]
