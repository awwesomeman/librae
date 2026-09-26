"""A close the venue cannot accept must not strand every other exit.

An ungrouped reduce/close that the adapter rejects as below the venue minimum
is skipped for that symbol, recorded, and alerted once, while the remaining
exits still go out. Entries and every other preparation error stay fail-closed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.core.run_config import RiskPolicy
from librae.core.strategy import OrderIntent, PositionState
from librae.live.executor import OrderBelowVenueMinimumError

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


def _trader(adapter, **kwargs):
    events: list = []
    alerts: list[dict] = []
    # Importing the class itself would make pytest collect its tests here too.
    trader = test_live_runner.TestLiveExecutionLifecycle()._make_trader(
        _HoldStrategy(),
        adapter,
        config=_test_cfg(mode="live", symbols=SYMBOLS),
        on_runtime_event=events.append,
        **kwargs,
    )
    trader._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
    trader._positions = {symbol: _position(symbol) for symbol in SYMBOLS}
    trader._last_prices = {symbol: 100.0 for symbol in SYMBOLS}
    return trader, events, alerts


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

        trader._record_equity(TEST_CLOCK_NOW, _bars())

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
