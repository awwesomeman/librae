"""Tests for OrderEvent generation in execute_order_intents and engine.

Verifies open/add/reduce/close events have correct
entry_price, remaining_quantity, pnl, net_return, and reason.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import OrderEvent, apply_execution_fill, execute_order_intents
from librae.core.strategy import Fill, OrderIntent, PositionState, Strategy

TS = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
ZERO_COST = CostModel.zero()


def _run(
    actions_list: list[OrderIntent],
    cost_model: CostModel = ZERO_COST,
    positions: dict[str, PositionState] | None = None,
    cash: float = 100_000.0,
):
    """Run execute_order_intents and return (events, trades, positions)."""
    if positions is None:
        positions = {}
    result = execute_order_intents(
        actions_list,
        positions,
        cash,
        TS,
        get_price=lambda sym, action: 100.0,
        get_cost_model=lambda sym: cost_model,
        primary_symbol="TEST",
    )
    return result.events, result.trades, positions


class TestOpenEvent:
    def test_buy_produces_open_event(self):
        events, _, _ = _run([OrderIntent(action="long", symbol="TEST")])
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "open"
        assert e.side == "long"
        assert e.symbol == "TEST"
        assert e.remaining_quantity > 0
        assert e.entry_price == 100.0

    def test_sell_produces_short_open(self):
        events, _, _ = _run([OrderIntent(action="short", symbol="TEST")])
        assert len(events) == 1
        assert events[0].event_type == "open"
        assert events[0].side == "short"

    def test_open_carries_reason(self):
        events, _, _ = _run([OrderIntent(action="long", symbol="TEST", reason="RSI oversold")])
        assert events[0].reason == "RSI oversold"

    def test_open_has_no_pnl(self):
        events, _, _ = _run([OrderIntent(action="long", symbol="TEST")])
        assert events[0].pnl is None
        assert events[0].net_return is None
        assert events[0].entry_at is None
        assert events[0].periods_held is None


class TestAddEvent:
    def test_scale_in_produces_add(self):
        positions = {
            "TEST": PositionState(
                symbol="TEST",
                side="long",
                entry_price=90.0,
                quantity=5.0,
                entry_at=TS,
                periods_held=3,
                entry_commission=0,
                entry_slippage=0,
                entry_tax=0,
                total_entry_cost=450.0,
            )
        }
        events, _, _ = _run(
            [OrderIntent(action="long", symbol="TEST", quantity=5.0)],
            positions=positions,
        )
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "add"
        assert e.fill_quantity == 5.0
        assert e.remaining_quantity == 10.0
        # entry_price = (450 + 500) / 10 = 95.0
        assert np.isclose(e.entry_price, 95.0)
        assert e.pnl is None


class TestReduceCloseEvents:
    def test_partial_close_produces_reduce(self):
        positions = {
            "TEST": PositionState(
                symbol="TEST",
                side="long",
                entry_price=80.0,
                quantity=10.0,
                entry_at=TS,
                periods_held=5,
                entry_commission=0,
                entry_slippage=0,
                entry_tax=0,
                total_entry_cost=800.0,
            )
        }
        events, trades, _ = _run(
            [OrderIntent(action="close", symbol="TEST", quantity=4.0)],
            positions=positions,
        )
        assert len(events) == 1
        assert len(trades) == 1
        e = events[0]
        assert e.event_type == "reduce"
        assert e.fill_quantity == 4.0
        assert e.remaining_quantity == 6.0
        assert e.entry_price == 80.0
        assert e.pnl is not None
        assert e.net_return is not None
        assert e.entry_at == TS
        assert e.periods_held == 5
        # PnL: (100 - 80) * 4 = 80
        assert np.isclose(e.pnl, 80.0)

    def test_full_close_produces_close(self):
        positions = {
            "TEST": PositionState(
                symbol="TEST",
                side="long",
                entry_price=80.0,
                quantity=10.0,
                entry_at=TS,
                periods_held=5,
                entry_commission=0,
                entry_slippage=0,
                entry_tax=0,
                total_entry_cost=800.0,
            )
        }
        events, _, pos = _run(
            [OrderIntent(action="close", symbol="TEST")],
            positions=positions,
        )
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "close"
        assert e.fill_quantity == 10.0
        assert e.remaining_quantity == 0.0
        assert "TEST" not in pos

    def test_event_costs_are_execution_side_only(self):
        cost_model = CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=1.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )
        positions: dict[str, PositionState] = {}
        open_events, _, _ = _run(
            [OrderIntent(action="long", symbol="TEST", quantity=1.0)],
            cost_model=cost_model,
            positions=positions,
        )
        close_events, trades, _ = _run(
            [OrderIntent(action="close", symbol="TEST")],
            cost_model=cost_model,
            positions=positions,
        )

        assert open_events[0].commission == 1.0
        assert close_events[0].commission == 1.0
        assert trades[0].commission == 2.0

    def test_confirmed_fill_event_costs_are_execution_side_only(self):
        positions = {
            "TEST": PositionState(
                symbol="TEST",
                side="long",
                entry_price=100.0,
                quantity=1.0,
                entry_at=TS,
                periods_held=1,
                entry_commission=1.0,
                entry_slippage=0.5,
                entry_tax=0.25,
                total_entry_cost=100.0,
            )
        }
        fill = Fill(
            symbol="TEST",
            side="short",
            price=110.0,
            quantity=1.0,
            commission=2.0,
            slippage=1.0,
            tax=0.5,
        )

        _, result = apply_execution_fill(
            positions,
            0.0,
            fill,
            TS,
            order_side="sell",
            cost_model=ZERO_COST,
        )

        assert result.events[0].commission == 2.0
        assert result.events[0].slippage == 1.0
        assert result.events[0].tax == 0.5
        assert result.trades[0].commission == 3.0
        assert result.trades[0].slippage == 1.5
        assert result.trades[0].tax == 0.75

    def test_close_qty_exceeds_position_clamped(self):
        """action.quantity > pos.quantity should be clamped, not inflate the event."""
        positions = {
            "TEST": PositionState(
                symbol="TEST",
                side="long",
                entry_price=80.0,
                quantity=10.0,
                entry_at=TS,
                periods_held=5,
                entry_commission=0,
                entry_slippage=0,
                entry_tax=0,
                total_entry_cost=800.0,
            )
        }
        events, trades, pos = _run(
            [OrderIntent(action="close", symbol="TEST", quantity=999.0)],
            positions=positions,
        )
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "close"
        assert e.fill_quantity == 10.0  # clamped, not 999
        assert e.remaining_quantity == 0.0
        assert trades[0].quantity == 10.0
        assert "TEST" not in pos

    def test_close_reason_carried(self):
        positions = {
            "TEST": PositionState(
                symbol="TEST",
                side="long",
                entry_price=80.0,
                quantity=10.0,
                entry_at=TS,
                periods_held=5,
                entry_commission=0,
                entry_slippage=0,
                entry_tax=0,
                total_entry_cost=800.0,
            )
        }
        events, _, _ = _run(
            [OrderIntent(action="close", symbol="TEST", reason="stop loss")],
            positions=positions,
        )
        assert events[0].reason == "stop loss"


class TestComplexLifecycle:
    """buy → buy → sell a bit → buy → sell all."""

    def test_full_lifecycle_events(self):
        positions: dict[str, PositionState] = {}
        all_events: list[OrderEvent] = []

        def run_at(actions, price, positions, periods_held_increment=0):
            for p in positions.values():
                p.periods_held += periods_held_increment
            result = execute_order_intents(
                actions,
                positions,
                1_000_000.0,
                TS,
                get_price=lambda sym, action: price,
                get_cost_model=lambda sym: ZERO_COST,
                primary_symbol="TEST",
            )
            all_events.extend(result.events)

        # 1. buy 10@100
        run_at([OrderIntent(action="long", symbol="TEST", quantity=10)], 100.0, positions)
        assert all_events[-1].event_type == "open"
        assert all_events[-1].remaining_quantity == 10.0

        # 2. buy 5@120 (scale in)
        run_at([OrderIntent(action="long", symbol="TEST", quantity=5)], 120.0, positions, 3)
        assert all_events[-1].event_type == "add"
        assert all_events[-1].remaining_quantity == 15.0
        assert np.isclose(all_events[-1].entry_price, (100 * 10 + 120 * 5) / 15)

        # 3. sell 3@130 (partial close)
        run_at([OrderIntent(action="close", symbol="TEST", quantity=3)], 130.0, positions, 2)
        assert all_events[-1].event_type == "reduce"
        assert all_events[-1].remaining_quantity == 12.0
        assert all_events[-1].pnl is not None

        # 4. buy 8@110 (scale in again)
        run_at([OrderIntent(action="long", symbol="TEST", quantity=8)], 110.0, positions, 1)
        assert all_events[-1].event_type == "add"
        assert all_events[-1].remaining_quantity == 20.0

        # 5. sell all @140 (full close)
        run_at([OrderIntent(action="close", symbol="TEST")], 140.0, positions, 5)
        assert all_events[-1].event_type == "close"
        assert all_events[-1].remaining_quantity == 0.0
        assert all_events[-1].pnl is not None

        assert len(all_events) == 5
        types = [e.event_type for e in all_events]
        assert types == ["open", "add", "reduce", "add", "close"]


class TestShortLifecycle:
    def test_short_open_add_close(self):
        positions: dict[str, PositionState] = {}
        all_events: list[OrderEvent] = []

        def run_at(actions, price, positions):
            result = execute_order_intents(
                actions,
                positions,
                1_000_000.0,
                TS,
                get_price=lambda sym, action: price,
                get_cost_model=lambda sym: ZERO_COST,
                primary_symbol="TEST",
            )
            all_events.extend(result.events)

        run_at([OrderIntent(action="short", symbol="TEST", quantity=10)], 100.0, positions)
        run_at([OrderIntent(action="short", symbol="TEST", quantity=5)], 110.0, positions)
        run_at([OrderIntent(action="close", symbol="TEST")], 90.0, positions)

        assert len(all_events) == 3
        assert all_events[0].event_type == "open"
        assert all_events[0].side == "short"
        assert all_events[1].event_type == "add"
        assert all_events[2].event_type == "close"
        assert all_events[2].remaining_quantity == 0.0
        # Short profit: (avg_entry - 90) * 15
        assert all_events[2].pnl > 0


class TestEngineIntegration:
    """Engine.run() produces order_events in BacktestResult."""

    def test_engine_produces_events(self):
        n = 50
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        prices = 100.0 + np.cumsum(np.random.default_rng(42).normal(0.5, 1, n))
        mi = pd.MultiIndex.from_arrays(
            [["TEST"] * n, idx],
            names=["symbol", "datetime"],
        )
        df = pd.DataFrame(
            {
                "open": prices,
                "high": prices * 1.001,
                "low": prices * 0.999,
                "close": prices,
                "volume": np.full(n, 100.0),
            },
            index=mi,
        )

        class BuyBar5CloseBar20(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 5 and ctx.symbol not in ctx.positions:
                    return [OrderIntent(action="long", symbol=ctx.symbol, reason="test entry")]
                if ctx.period_index == 20 and ctx.symbol in ctx.positions:
                    return [OrderIntent(action="close", symbol=ctx.symbol, reason="test exit")]
                return []

        bt = Backtest(
            df,
            BuyBar5CloseBar20(),
            initial_balance=10_000,
            cost_model=ZERO_COST,
            data_source="test",
        )
        result = bt.run()

        # Should have open + close events (+ possible force_close at end)
        assert len(result.order_events) >= 2
        types = [e.event_type for e in result.order_events]
        assert "open" in types
        assert "close" in types

        # Verify build_output includes events
        output = bt.build_output()
        assert len(output.order_events) >= 2
        assert output.order_events[0].event_id.startswith(bt._run_id)
