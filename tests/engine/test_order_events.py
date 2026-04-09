"""Tests for OrderEvent generation in process_actions and engine.

Verifies open/add/reduce/close events have correct
entry_price, position_quantity, pnl, net_return, and reason.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import OrderEvent, process_actions
from librae.core.strategy import Action, BaseStrategy, PositionState


TS = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
ZERO_COST = CostModel.zero()


def _run(actions_list: list[Action], cost_model: CostModel = ZERO_COST,
         positions: dict[str, PositionState] | None = None, cash: float = 100_000.0):
    """Run process_actions and return (events, trades, positions)."""
    if positions is None:
        positions = {}
    result = process_actions(
        actions_list, positions, cash, TS,
        get_price=lambda sym, action: 100.0,
        get_cost_model=lambda sym: cost_model,
        primary_symbol="TEST",
    )
    return result.events, result.trades, positions


class TestOpenEvent:
    def test_buy_produces_open_event(self):
        events, _, _ = _run([Action(type="buy", symbol="TEST")])
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "open"
        assert e.side == "long"
        assert e.symbol == "TEST"
        assert e.position_quantity > 0
        assert e.entry_price == 100.0

    def test_sell_produces_short_open(self):
        events, _, _ = _run([Action(type="sell", symbol="TEST")])
        assert len(events) == 1
        assert events[0].event_type == "open"
        assert events[0].side == "short"

    def test_open_carries_reason(self):
        events, _, _ = _run([Action(type="buy", symbol="TEST", reason="RSI oversold")])
        assert events[0].reason == "RSI oversold"

    def test_open_has_no_pnl(self):
        events, _, _ = _run([Action(type="buy", symbol="TEST")])
        assert events[0].pnl is None
        assert events[0].net_return is None
        assert events[0].entry_ts is None
        assert events[0].holding_periods is None


class TestAddEvent:
    def test_scale_in_produces_add(self):
        positions = {"TEST": PositionState(
            symbol="TEST", side="long", entry_price=90.0, quantity=5.0,
            entry_ts=TS, holding_periods=3, entry_commission=0, entry_slippage=0, entry_tax=0,
            total_entry_cost=450.0,
        )}
        events, _, _ = _run(
            [Action(type="buy", symbol="TEST", quantity=5.0)],
            positions=positions,
        )
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "add"
        assert e.quantity == 5.0
        assert e.position_quantity == 10.0
        # entry_price = (450 + 500) / 10 = 95.0
        assert np.isclose(e.entry_price, 95.0)
        assert e.pnl is None


class TestReduceCloseEvents:
    def test_partial_close_produces_reduce(self):
        positions = {"TEST": PositionState(
            symbol="TEST", side="long", entry_price=80.0, quantity=10.0,
            entry_ts=TS, holding_periods=5, entry_commission=0, entry_slippage=0, entry_tax=0,
            total_entry_cost=800.0,
        )}
        events, trades, _ = _run(
            [Action(type="close", symbol="TEST", quantity=4.0)],
            positions=positions,
        )
        assert len(events) == 1
        assert len(trades) == 1
        e = events[0]
        assert e.event_type == "reduce"
        assert e.quantity == 4.0
        assert e.position_quantity == 6.0
        assert e.entry_price == 80.0
        assert e.pnl is not None
        assert e.net_return is not None
        assert e.entry_ts == TS
        assert e.holding_periods == 5
        # PnL: (100 - 80) * 4 = 80
        assert np.isclose(e.pnl, 80.0)

    def test_full_close_produces_close(self):
        positions = {"TEST": PositionState(
            symbol="TEST", side="long", entry_price=80.0, quantity=10.0,
            entry_ts=TS, holding_periods=5, entry_commission=0, entry_slippage=0, entry_tax=0,
            total_entry_cost=800.0,
        )}
        events, _, pos = _run(
            [Action(type="close", symbol="TEST")],
            positions=positions,
        )
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "close"
        assert e.quantity == 10.0
        assert e.position_quantity == 0.0
        assert "TEST" not in pos

    def test_close_qty_exceeds_position_clamped(self):
        """action.quantity > pos.quantity should be clamped, not inflate the event."""
        positions = {"TEST": PositionState(
            symbol="TEST", side="long", entry_price=80.0, quantity=10.0,
            entry_ts=TS, holding_periods=5, entry_commission=0, entry_slippage=0, entry_tax=0,
            total_entry_cost=800.0,
        )}
        events, trades, pos = _run(
            [Action(type="close", symbol="TEST", quantity=999.0)],
            positions=positions,
        )
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "close"
        assert e.quantity == 10.0  # clamped, not 999
        assert e.position_quantity == 0.0
        assert trades[0].quantity == 10.0
        assert "TEST" not in pos

    def test_close_reason_carried(self):
        positions = {"TEST": PositionState(
            symbol="TEST", side="long", entry_price=80.0, quantity=10.0,
            entry_ts=TS, holding_periods=5, entry_commission=0, entry_slippage=0, entry_tax=0,
            total_entry_cost=800.0,
        )}
        events, _, _ = _run(
            [Action(type="close", symbol="TEST", reason="stop loss")],
            positions=positions,
        )
        assert events[0].reason == "stop loss"


class TestComplexLifecycle:
    """buy → buy → sell a bit → buy → sell all."""

    def test_full_lifecycle_events(self):
        positions: dict[str, PositionState] = {}
        all_events: list[OrderEvent] = []

        def run_at(actions, price, positions, holding_periods_increment=0):
            for p in positions.values():
                p.holding_periods += holding_periods_increment
            result = process_actions(
                actions, positions, 1_000_000.0, TS,
                get_price=lambda sym, action: price,
                get_cost_model=lambda sym: ZERO_COST,
                primary_symbol="TEST",
            )
            all_events.extend(result.events)

        # 1. buy 10@100
        run_at([Action(type="buy", symbol="TEST", quantity=10)], 100.0, positions)
        assert all_events[-1].event_type == "open"
        assert all_events[-1].position_quantity == 10.0

        # 2. buy 5@120 (scale in)
        run_at([Action(type="buy", symbol="TEST", quantity=5)], 120.0, positions, 3)
        assert all_events[-1].event_type == "add"
        assert all_events[-1].position_quantity == 15.0
        assert np.isclose(all_events[-1].entry_price, (100 * 10 + 120 * 5) / 15)

        # 3. sell 3@130 (partial close)
        run_at([Action(type="close", symbol="TEST", quantity=3)], 130.0, positions, 2)
        assert all_events[-1].event_type == "reduce"
        assert all_events[-1].position_quantity == 12.0
        assert all_events[-1].pnl is not None

        # 4. buy 8@110 (scale in again)
        run_at([Action(type="buy", symbol="TEST", quantity=8)], 110.0, positions, 1)
        assert all_events[-1].event_type == "add"
        assert all_events[-1].position_quantity == 20.0

        # 5. sell all @140 (full close)
        run_at([Action(type="close", symbol="TEST")], 140.0, positions, 5)
        assert all_events[-1].event_type == "close"
        assert all_events[-1].position_quantity == 0.0
        assert all_events[-1].pnl is not None

        assert len(all_events) == 5
        types = [e.event_type for e in all_events]
        assert types == ["open", "add", "reduce", "add", "close"]


class TestShortLifecycle:
    def test_short_open_add_close(self):
        positions: dict[str, PositionState] = {}
        all_events: list[OrderEvent] = []

        def run_at(actions, price, positions):
            result = process_actions(
                actions, positions, 1_000_000.0, TS,
                get_price=lambda sym, action: price,
                get_cost_model=lambda sym: ZERO_COST,
                primary_symbol="TEST",
            )
            all_events.extend(result.events)

        run_at([Action(type="sell", symbol="TEST", quantity=10)], 100.0, positions)
        run_at([Action(type="sell", symbol="TEST", quantity=5)], 110.0, positions)
        run_at([Action(type="close", symbol="TEST")], 90.0, positions)

        assert len(all_events) == 3
        assert all_events[0].event_type == "open"
        assert all_events[0].side == "short"
        assert all_events[1].event_type == "add"
        assert all_events[2].event_type == "close"
        assert all_events[2].position_quantity == 0.0
        # Short profit: (avg_entry - 90) * 15
        assert all_events[2].pnl > 0


class TestEngineIntegration:
    """Engine.run() produces order_events in BacktestResult."""

    def test_engine_produces_events(self):
        n = 50
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        prices = 100.0 + np.cumsum(np.random.default_rng(42).normal(0.5, 1, n))
        mi = pd.MultiIndex.from_arrays(
            [["TEST"] * n, idx], names=["symbol", "datetime"],
        )
        df = pd.DataFrame({
            "open": prices, "high": prices * 1.001,
            "low": prices * 0.999, "close": prices,
            "volume": np.full(n, 100.0),
        }, index=mi)

        class BuyBar5CloseBar20(BaseStrategy):
            def on_bar(self, ctx):
                if ctx.period_index == 5 and ctx.symbol not in ctx.positions:
                    return [Action(type="buy", symbol=ctx.symbol, reason="test entry")]
                if ctx.period_index == 20 and ctx.symbol in ctx.positions:
                    return [Action(type="close", symbol=ctx.symbol, reason="test exit")]
                return []

        bt = Backtest(df, BuyBar5CloseBar20(), initial_balance=10_000,
                      cost_model=ZERO_COST, data_source="test")
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
