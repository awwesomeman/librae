"""Tests for engine-level risk controls: max-position cap + max-drawdown circuit breaker."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    REASON_FORCE_CLOSE,
    _cap_fill_to_notional,
    liquidate_all,
    process_actions,
)
from librae.core.strategy import Action, BaseStrategy, Context, Fill, PositionState
from tests.conftest import make_test_cfg


# ---------------------------------------------------------------------------
# Helpers — mirrors tests/engine/test_stop_targets.py / test_position_scaling.py
# ---------------------------------------------------------------------------


def _zero_cost() -> CostModel:
    return CostModel.zero()


def _make_pos(
    symbol: str = "TEST", side: str = "long", entry_price: float = 100.0, quantity: float = 10.0,
) -> PositionState:
    return PositionState(
        symbol=symbol, side=side, entry_price=entry_price, quantity=quantity,
        entry_at=datetime(2026, 1, 1, tzinfo=timezone.utc), periods_held=0,
        entry_commission=0.0, entry_slippage=0.0, entry_tax=0.0,
        total_entry_cost=entry_price * quantity,
    )


def _make_fill(price: float = 100.0, quantity: float = 10.0, side: str = "long") -> Fill:
    return Fill(symbol="TEST", side=side, price=price, quantity=quantity, commission=0.0, slippage=0.0, tax=0.0)


def _make_multiindex_df(bars: list[dict[str, float]], symbol: str = "BTCUSDT") -> pd.DataFrame:
    n = len(bars)
    dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    idx = pd.MultiIndex.from_arrays([[symbol] * n, dt], names=["symbol", "datetime"])
    df = pd.DataFrame(bars, index=idx)
    df["volume"] = 100.0
    return df


TS = datetime(2026, 1, 10, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Unit tests: _cap_fill_to_notional
# ---------------------------------------------------------------------------


class TestCapFillToNotional:

    def test_caps_a_fresh_fill(self):
        fill = _make_fill(price=100.0, quantity=10.0)
        capped = _cap_fill_to_notional(fill, existing_qty=0.0, cost_model=_zero_cost(), max_notional=300.0)
        assert capped.quantity == pytest.approx(3.0)

    def test_caps_a_scale_in_against_existing_notional(self):
        fill = _make_fill(price=100.0, quantity=10.0)
        capped = _cap_fill_to_notional(fill, existing_qty=2.0, cost_model=_zero_cost(), max_notional=500.0)
        # room = 500 - 2*100 = 300 -> 3 more units
        assert capped.quantity == pytest.approx(3.0)

    def test_no_room_left_returns_none(self):
        fill = _make_fill(price=100.0, quantity=10.0)
        capped = _cap_fill_to_notional(fill, existing_qty=5.0, cost_model=_zero_cost(), max_notional=500.0)
        assert capped is None

    def test_under_cap_returns_fill_unchanged(self):
        fill = _make_fill(price=100.0, quantity=1.0)
        capped = _cap_fill_to_notional(fill, existing_qty=0.0, cost_model=_zero_cost(), max_notional=1000.0)
        assert capped is fill


# ---------------------------------------------------------------------------
# Unit tests: liquidate_all
# ---------------------------------------------------------------------------


class TestLiquidateAll:

    def test_closes_all_positions_and_sums_cash_delta(self):
        positions = {
            "A": _make_pos(symbol="A", side="long", entry_price=100.0, quantity=1.0),
            "B": _make_pos(symbol="B", side="short", entry_price=100.0, quantity=1.0),
        }
        bars = {"A": {"close": 110.0}, "B": {"close": 90.0}}
        result = liquidate_all(
            positions, bars, TS, get_cost_model=lambda s: _zero_cost(), reason=REASON_DRAWDOWN_BREACH,
        )
        assert positions == {}
        assert len(result.trades) == 2
        assert all(e.reason == REASON_DRAWDOWN_BREACH for e in result.events)
        # long +10 gain, short +10 gain (price moved against short's loss side... both favorable here)
        assert result.cash_delta == pytest.approx(220.0)

    def test_missing_bar_uses_fallback_price(self):
        positions = {"A": _make_pos(symbol="A", entry_price=100.0, quantity=1.0)}
        result = liquidate_all(
            positions, {}, TS, get_cost_model=lambda s: _zero_cost(), reason=REASON_FORCE_CLOSE,
            fallback_price=lambda sym, pos: pos.entry_price,
        )
        assert positions == {}
        assert result.trades[0].exit_price == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Unit tests: process_actions respects max_position_notional
# ---------------------------------------------------------------------------


class TestProcessActionsPositionCap:

    def test_oversized_open_gets_clamped(self):
        actions = [Action(type="long", symbol="TEST")]
        result = process_actions(
            actions, {}, 10_000.0, TS,
            get_price=lambda s, a: 100.0,
            get_cost_model=lambda s: _zero_cost(),
            primary_symbol="TEST",
            max_position_notional=300.0,
        )
        assert len(result.events) == 1
        assert result.events[0].fill_quantity == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Integration tests: full Backtest run
# ---------------------------------------------------------------------------


class OpenOnceStrategy(BaseStrategy):
    """Opens a long at bar 1, never closes itself — isolates engine-enforced exits."""

    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.period_index == 1 and ctx.symbol not in ctx.positions:
            return [Action(type="long", symbol=ctx.symbol)]
        return []


class TestMaxDrawdownBreaker:

    def test_breach_flattens_and_halts(self):
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 60, "high": 65, "low": 55, "close": 60},   # crater -> breach
            {"open": 60, "high": 61, "low": 59, "close": 60},   # halted: stays flat
        ]
        cfg = make_test_cfg(
            mode="backtest", initial_balance=10_000.0, params={"max_drawdown_pct": 0.2},
        )
        bt = Backtest(_make_multiindex_df(bars), OpenOnceStrategy(), cfg=cfg, cost_model=_zero_cost())
        result = bt.run()

        close_events = [e for e in result.order_events if e.event_type == "close"]
        assert len(close_events) == 1
        assert close_events[0].reason == REASON_DRAWDOWN_BREACH
        assert len(result.trades) == 1  # no further trades after the breach

        assert len(result.equity_curve) == len(bars)  # curve continues to the end, flat
        assert result.equity_curve[-1].equity == pytest.approx(result.equity_curve[3].equity)

    def test_no_breach_when_disabled(self):
        """Same crash, no max_drawdown_pct set -> position rides it out (baseline sanity check)."""
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 60, "high": 65, "low": 55, "close": 60},
            {"open": 60, "high": 61, "low": 59, "close": 60},
        ]
        bt = Backtest(_make_multiindex_df(bars), OpenOnceStrategy(), cost_model=_zero_cost())
        result = bt.run()

        close_events = [e for e in result.order_events if e.event_type == "close"]
        assert len(close_events) == 1
        assert close_events[0].reason == REASON_FORCE_CLOSE  # only closed by end-of-run liquidation


class TestMaxPositionCap:

    def test_open_clamped_to_pct_of_equity(self):
        bars = [
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
            {"open": 100, "high": 101, "low": 99, "close": 100},
        ]
        cfg = make_test_cfg(
            mode="backtest", initial_balance=10_000.0, params={"max_position_pct": 0.3},
        )
        bt = Backtest(_make_multiindex_df(bars), OpenOnceStrategy(), cfg=cfg, cost_model=_zero_cost())
        result = bt.run()

        assert len(result.trades) == 1
        # Uncapped, all ~10_000 cash at price 100 would size ~100 units.
        # max_position_pct=0.3 against last-known equity (10_000) caps notional at 3_000 -> 30 units.
        assert result.trades[0].quantity == pytest.approx(30.0)
