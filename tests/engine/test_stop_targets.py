"""Tests for stop-loss / take-profit: unit-level trigger logic + full-engine integration."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_FORCE_CLOSE,
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
    resolve_stop_exit,
)
from librae.core.strategy import Action, BaseStrategy, Context, PositionState


# ---------------------------------------------------------------------------
# Helpers — naming mirrors tests/engine/test_position_scaling.py and
# tests/engine/test_backtest_v2.py so the shared fixtures stay recognizable
# across the engine test suite.
# ---------------------------------------------------------------------------


def _zero_cost() -> CostModel:
    return CostModel.zero()


def _make_pos(side="long", stop=None, tp=None) -> PositionState:
    return PositionState(
        symbol="TEST", side=side, entry_price=100.0, quantity=1.0,
        entry_at=datetime(2026, 1, 1, tzinfo=timezone.utc), periods_held=0,
        entry_commission=0.0, entry_slippage=0.0, entry_tax=0.0,
        total_entry_cost=100.0, stop_price=stop, take_profit_price=tp,
    )


def _make_multiindex_df(bars: list[dict[str, float]], symbol: str = "BTCUSDT") -> pd.DataFrame:
    n = len(bars)
    dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    idx = pd.MultiIndex.from_arrays([[symbol] * n, dt], names=["symbol", "datetime"])
    df = pd.DataFrame(bars, index=idx)
    df["volume"] = 100.0
    return df


# ---------------------------------------------------------------------------
# Unit tests: resolve_stop_exit
# ---------------------------------------------------------------------------


class TestResolveStopExit:

    def test_long_stop_hit_fills_at_stop_price(self):
        pos = _make_pos(side="long", stop=95.0)
        bar = {"open": 98.0, "high": 99.0, "low": 94.0, "close": 96.0}
        assert resolve_stop_exit(pos, bar) == (95.0, REASON_STOP_LOSS)

    def test_long_stop_gap_down_fills_at_open(self):
        """Gap through the stop — real fill is worse than the stop price."""
        pos = _make_pos(side="long", stop=95.0)
        bar = {"open": 90.0, "high": 91.0, "low": 88.0, "close": 89.0}
        assert resolve_stop_exit(pos, bar) == (90.0, REASON_STOP_LOSS)

    def test_long_take_profit_fills_exactly_at_target(self):
        pos = _make_pos(side="long", tp=110.0)
        bar = {"open": 105.0, "high": 112.0, "low": 104.0, "close": 108.0}
        assert resolve_stop_exit(pos, bar) == (110.0, REASON_TAKE_PROFIT)

    def test_short_stop_hit_on_high(self):
        pos = _make_pos(side="short", stop=105.0)
        bar = {"open": 102.0, "high": 106.0, "low": 101.0, "close": 104.0}
        assert resolve_stop_exit(pos, bar) == (105.0, REASON_STOP_LOSS)

    def test_short_take_profit_hit_on_low(self):
        pos = _make_pos(side="short", tp=90.0)
        bar = {"open": 95.0, "high": 96.0, "low": 88.0, "close": 91.0}
        assert resolve_stop_exit(pos, bar) == (90.0, REASON_TAKE_PROFIT)

    def test_stop_loss_wins_when_both_hit_same_bar(self):
        pos = _make_pos(side="long", stop=95.0, tp=110.0)
        bar = {"open": 98.0, "high": 111.0, "low": 94.0, "close": 100.0}
        assert resolve_stop_exit(pos, bar) == (95.0, REASON_STOP_LOSS)

    def test_no_trigger_returns_none(self):
        pos = _make_pos(side="long", stop=95.0, tp=110.0)
        bar = {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
        assert resolve_stop_exit(pos, bar) is None

    def test_no_stop_or_target_set_returns_none(self):
        pos = _make_pos(side="long")
        bar = {"open": 50.0, "high": 200.0, "low": 1.0, "close": 100.0}
        assert resolve_stop_exit(pos, bar) is None


# ---------------------------------------------------------------------------
# Integration tests: full Backtest run
# ---------------------------------------------------------------------------


class OpenWithStopAtBar1(BaseStrategy):
    """Open long at bar 1 with a fixed stop/target; never closes itself."""

    def __init__(self, stop_price: float | None, take_profit_price: float | None):
        self.stop_price = stop_price
        self.take_profit_price = take_profit_price

    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.period_index == 1 and ctx.symbol not in ctx.positions:
            return [Action(
                type="long", symbol=ctx.symbol,
                stop_price=self.stop_price, take_profit_price=self.take_profit_price,
            )]
        return []


class TestStopTargetIntegration:

    def test_stop_loss_force_closes_before_strategy_would(self):
        # Fill at bar1's open=100 -> stop set at 90. Bar3 gaps down through it.
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 80, "high": 85, "low": 78, "close": 82},
            {"open": 82, "high": 83, "low": 81, "close": 82},
        ]
        strategy = OpenWithStopAtBar1(stop_price=90.0, take_profit_price=None)
        bt = Backtest(_make_multiindex_df(bars), strategy, cost_model=_zero_cost())
        result = bt.run()

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_price == pytest.approx(80.0)  # gapped through -> fills at open
        close_events = [e for e in result.order_events if e.event_type == "close"]
        assert len(close_events) == 1
        assert close_events[0].reason == REASON_STOP_LOSS

    def test_take_profit_force_closes_at_target(self):
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 108, "high": 115, "low": 107, "close": 112},
            {"open": 112, "high": 113, "low": 111, "close": 112},
        ]
        strategy = OpenWithStopAtBar1(stop_price=None, take_profit_price=110.0)
        bt = Backtest(_make_multiindex_df(bars), strategy, cost_model=_zero_cost())
        result = bt.run()

        assert len(result.trades) == 1
        assert result.trades[0].exit_price == pytest.approx(110.0)
        close_events = [e for e in result.order_events if e.event_type == "close"]
        assert close_events[0].reason == REASON_TAKE_PROFIT

    def test_no_stop_target_set_runs_to_force_close(self):
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 50, "high": 60, "low": 40, "close": 55},
        ]
        strategy = OpenWithStopAtBar1(stop_price=None, take_profit_price=None)
        bt = Backtest(_make_multiindex_df(bars), strategy, cost_model=_zero_cost())
        result = bt.run()

        close_events = [e for e in result.order_events if e.event_type == "close"]
        assert len(close_events) == 1
        assert close_events[0].reason == REASON_FORCE_CLOSE
