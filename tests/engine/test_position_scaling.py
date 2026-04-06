"""Tests for position scaling, partial close, and short positions.

Covers executor unit tests (#1-5), short integration (#6-9),
scaling integration (#10-14), and edge cases (#15-24).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from librae.core.cost_model import CostModel
from librae.core.executor import (
    ActionResults,
    build_trade_result,
    calc_trade_pnl,
    close_position,
    process_actions,
    reduce_position,
    scale_into_position,
)
from librae.core.strategy import Action, BaseStrategy, Context, Fill, PositionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _zero_cost() -> CostModel:
    return CostModel.zero()


def _crypto_cost() -> CostModel:
    return CostModel(
        multiplier=1.0, commission_rate=0.001, min_commission=0.0,
        slippage_ticks=0.0, tick_size=0.01, transaction_tax=0.0,
    )


def _tw_futures_cost() -> CostModel:
    """Taiwan futures: tax on sell side only."""
    return CostModel(
        multiplier=1.0, commission_rate=0.0, min_commission=0.0,
        slippage_ticks=0.0, tick_size=1.0, transaction_tax=0.00002,
    )


def _make_pos(
    symbol: str = "TEST",
    side: str = "long",
    entry_price: float = 100.0,
    quantity: float = 10.0,
    periods_held: int = 5,
    cm: CostModel | None = None,
) -> PositionState:
    cm = cm or _zero_cost()
    return PositionState(
        symbol=symbol, side=side,
        entry_price=entry_price, quantity=quantity,
        entry_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        periods_held=periods_held,
        entry_commission=cm.calc_commission(entry_price, quantity),
        entry_slippage=cm.calc_slippage(quantity),
        total_entry_cost=entry_price * quantity * cm.multiplier,
    )


def _make_fill(
    symbol: str = "TEST",
    side: str = "long",
    price: float = 120.0,
    quantity: float = 5.0,
    cm: CostModel | None = None,
) -> Fill:
    cm = cm or _zero_cost()
    return Fill(
        symbol=symbol, side=side, price=price, quantity=quantity,
        commission=cm.calc_commission(price, quantity),
        slippage=cm.calc_slippage(quantity),
        tax=cm.calc_tax(price, quantity, is_sell=(side == "short")),
    )


TS = datetime(2026, 1, 10, tzinfo=timezone.utc)


def _run_actions(
    actions: list[Action],
    positions: dict[str, PositionState] | None = None,
    cash: float = 100_000.0,
    prices: dict[str, float] | None = None,
    cm: CostModel | None = None,
) -> ActionResults:
    if positions is None:
        positions = {}
    if prices is None:
        prices = {"TEST": 100.0}
    cm = cm or _zero_cost()
    return process_actions(
        actions, positions, cash, TS,
        get_price=lambda s, action: prices.get(s),
        get_cost_model=lambda s: cm,
        primary_symbol="TEST",
    )


# ===========================================================================
# Executor unit tests (#1-5)
# ===========================================================================


class TestScaleIntoPosition:
    """#1, #2: scale_into_position updates avg price and accumulates costs."""

    def test_weighted_avg_price(self):
        cm = _zero_cost()
        pos = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        fill = _make_fill(price=120.0, quantity=10.0, cm=cm)

        scale_into_position(pos, fill, cm)

        assert pos.quantity == 20.0
        assert pos.entry_price == pytest.approx(110.0)  # (100*10 + 120*10) / 20

    def test_accumulates_costs(self):
        cm = _crypto_cost()
        pos = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        initial_comm = pos.entry_commission
        fill = _make_fill(price=120.0, quantity=5.0, cm=cm)

        scale_into_position(pos, fill, cm)

        assert pos.entry_commission == pytest.approx(initial_comm + fill.commission)

    def test_three_adds_weighted_mean(self):
        """#11: buy 5@100, buy 3@120, buy 2@150 → avg = (500+360+300)/10 = 116."""
        cm = _zero_cost()
        pos = _make_pos(entry_price=100.0, quantity=5.0, cm=cm)

        scale_into_position(pos, _make_fill(price=120.0, quantity=3.0, cm=cm), cm)
        scale_into_position(pos, _make_fill(price=150.0, quantity=2.0, cm=cm), cm)

        assert pos.quantity == 10.0
        assert pos.entry_price == pytest.approx(116.0)


class TestReducePosition:
    """#3: reduce_position pro-rates costs."""

    def test_pro_rates_costs(self):
        cm = _crypto_cost()
        pos = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        original_comm = pos.entry_commission

        reduce_position(pos, 5.0)

        assert pos.quantity == 5.0
        assert pos.entry_commission == pytest.approx(original_comm * 0.5)

    def test_entry_price_unchanged(self):
        pos = _make_pos(entry_price=100.0, quantity=10.0)
        reduce_position(pos, 3.0)
        assert pos.entry_price == pytest.approx(100.0)


class TestClosePosition:
    """#4, #5: partial and full close."""

    def test_partial_close_pnl(self):
        cm = _zero_cost()
        pos = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        pnl, proceeds, fully_closed = close_position(pos, 110.0, cm, quantity=5.0)

        assert not fully_closed
        assert pnl.gross_pnl == pytest.approx(50.0)  # (110-100)*5

    def test_full_close_equals_quantity_none(self):
        cm = _zero_cost()
        pos1 = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        pos2 = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)

        pnl_full, _, closed_full = close_position(pos1, 110.0, cm)
        pnl_qty, _, closed_qty = close_position(pos2, 110.0, cm, quantity=10.0)

        assert closed_full and closed_qty
        assert pnl_full.net_pnl == pytest.approx(pnl_qty.net_pnl)


# ===========================================================================
# Short tests (#6-9)
# ===========================================================================


class TestShortPositions:

    def test_short_profitable(self):
        """#6: sell@100, close@90 → profit."""
        cm = _zero_cost()
        pos = _make_pos(side="short", entry_price=100.0, quantity=1.0, cm=cm)
        pnl, proceeds, _ = close_position(pos, 90.0, cm)

        assert pnl.gross_pnl == pytest.approx(10.0)
        assert proceeds == pytest.approx(110.0)  # collateral 100 + profit 10

    def test_short_losing(self):
        """#7: sell@100, close@110 → loss."""
        cm = _zero_cost()
        pos = _make_pos(side="short", entry_price=100.0, quantity=1.0, cm=cm)
        pnl, proceeds, _ = close_position(pos, 110.0, cm)

        assert pnl.gross_pnl == pytest.approx(-10.0)
        assert proceeds == pytest.approx(90.0)  # collateral 100 - loss 10

    def test_short_round_trip_zero_cost(self):
        """#8: short round-trip at same price → cash unchanged."""
        cm = _zero_cost()
        initial_cash = 100_000.0
        outlay = cm.estimate_entry_outlay(100.0, 1.0)
        cash_after_entry = initial_cash - outlay

        pos = _make_pos(side="short", entry_price=100.0, quantity=1.0, cm=cm)
        _, proceeds, _ = close_position(pos, 100.0, cm)

        assert cash_after_entry + proceeds == pytest.approx(initial_cash)

    def test_short_close_no_tax(self):
        """#9: buy-to-cover should NOT incur transaction tax."""
        cm = _tw_futures_cost()
        pos = _make_pos(side="short", entry_price=20000.0, quantity=1.0, cm=cm)
        pnl, _, _ = close_position(pos, 19900.0, cm)

        assert pnl.exit_tax == 0.0  # buy-to-cover is not a sell


# ===========================================================================
# Scaling integration tests (#10-14)
# ===========================================================================


class TestScalingIntegration:

    def test_long_scale_and_close(self):
        """#10: buy@100, add@120, close@130 → correct PnL."""
        positions: dict[str, PositionState] = {}
        actions = [Action(type="buy", symbol="TEST", quantity=10.0)]
        result = _run_actions(actions, positions, prices={"TEST": 100.0})
        assert "TEST" in positions
        assert result.cash_delta == pytest.approx(-100.0 * 10.0)

        # Scale in
        actions2 = [Action(type="buy", symbol="TEST", quantity=5.0)]
        result2 = _run_actions(actions2, positions, cash=100_000 + result.cash_delta, prices={"TEST": 120.0})
        assert positions["TEST"].quantity == 15.0
        expected_avg = (100.0 * 10.0 + 120.0 * 5.0) / 15.0
        assert positions["TEST"].entry_price == pytest.approx(expected_avg)

        # Close all
        actions3 = [Action(type="close", symbol="TEST")]
        result3 = _run_actions(actions3, positions, cash=100_000 + result.cash_delta + result2.cash_delta, prices={"TEST": 130.0})
        assert "TEST" not in positions
        assert len(result3.trades) == 1
        assert result3.trades[0].gross_pnl == pytest.approx((130.0 - expected_avg) * 15.0)

    def test_scale_partial_close_then_full(self):
        """#12: add, partial close, then close rest."""
        cm = _zero_cost()
        positions: dict[str, PositionState] = {}
        _run_actions([Action(type="buy", symbol="TEST", quantity=10.0)], positions, prices={"TEST": 100.0}, cm=cm)

        # Partial close 4
        result = _run_actions(
            [Action(type="close", symbol="TEST", quantity=4.0)],
            positions, prices={"TEST": 120.0}, cm=cm,
        )
        assert len(result.trades) == 1
        assert result.trades[0].quantity == 4.0
        assert result.trades[0].gross_pnl == pytest.approx(80.0)  # (120-100)*4
        assert positions["TEST"].quantity == pytest.approx(6.0)

        # Close rest
        result2 = _run_actions(
            [Action(type="close", symbol="TEST")],
            positions, prices={"TEST": 130.0}, cm=cm,
        )
        assert "TEST" not in positions
        assert result2.trades[0].gross_pnl == pytest.approx(180.0)  # (130-100)*6

    def test_two_partial_then_full_no_penny_leak(self):
        """#13: cumulative PnL from partials matches single full close."""
        cm = _zero_cost()

        # Single full close
        pos_full = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        pnl_full, _, _ = close_position(pos_full, 150.0, cm)

        # Two partials + rest
        pos_parts = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        pnl1, _, _ = close_position(pos_parts, 150.0, cm, quantity=3.0)
        reduce_position(pos_parts, 3.0)
        pnl2, _, _ = close_position(pos_parts, 150.0, cm, quantity=4.0)
        reduce_position(pos_parts, 4.0)
        pnl3, _, _ = close_position(pos_parts, 150.0, cm)

        total_pnl = pnl1.net_pnl + pnl2.net_pnl + pnl3.net_pnl
        assert total_pnl == pytest.approx(pnl_full.net_pnl)

    def test_short_scale_partial_close(self):
        """#14: short scaling + partial close."""
        cm = _zero_cost()
        positions: dict[str, PositionState] = {}

        # Open short
        _run_actions([Action(type="sell", symbol="TEST", quantity=5.0)], positions, prices={"TEST": 100.0}, cm=cm)
        assert positions["TEST"].side == "short"

        # Scale short
        _run_actions([Action(type="sell", symbol="TEST", quantity=3.0)], positions, prices={"TEST": 110.0}, cm=cm)
        assert positions["TEST"].quantity == 8.0

        # Partial close (buy-to-cover 4)
        result = _run_actions(
            [Action(type="close", symbol="TEST", quantity=4.0)],
            positions, prices={"TEST": 90.0}, cm=cm,
        )
        assert positions["TEST"].quantity == pytest.approx(4.0)
        assert result.trades[0].gross_pnl > 0  # price dropped, short profits


# ===========================================================================
# Edge cases (#15-24)
# ===========================================================================


class TestEdgeCases:

    def test_scale_without_quantity_rejected(self):
        """#15: scaling requires explicit quantity."""
        positions: dict[str, PositionState] = {}
        _run_actions([Action(type="buy", symbol="TEST", quantity=10.0)], positions, prices={"TEST": 100.0})

        # Try to scale without quantity
        result = _run_actions([Action(type="buy", symbol="TEST")], positions, prices={"TEST": 120.0})
        assert positions["TEST"].quantity == 10.0  # unchanged
        assert result.cash_delta == 0.0

    def test_buy_while_short_rejected(self):
        """#16: opposite-side action rejected."""
        positions: dict[str, PositionState] = {}
        _run_actions([Action(type="sell", symbol="TEST", quantity=5.0)], positions, prices={"TEST": 100.0})

        result = _run_actions([Action(type="buy", symbol="TEST", quantity=5.0)], positions, prices={"TEST": 90.0})
        assert positions["TEST"].side == "short"  # unchanged
        assert result.cash_delta == 0.0

    def test_partial_close_clamps_to_position(self):
        """#17: close more than held → clamp to full close."""
        cm = _zero_cost()
        pos = _make_pos(quantity=10.0, cm=cm)
        pnl, _, fully_closed = close_position(pos, 110.0, cm, quantity=999.0)

        assert fully_closed
        assert pnl.gross_pnl == pytest.approx(100.0)  # (110-100)*10, not *999

    def test_zero_quantity_close_rejected(self):
        """#18: close with quantity=0 does nothing."""
        cm = _zero_cost()
        pos = _make_pos(cm=cm)
        pnl, proceeds, fully_closed = close_position(pos, 110.0, cm, quantity=0.0)

        assert not fully_closed
        assert proceeds == 0.0

    def test_tiny_quantity_pnl_non_negative_costs(self):
        """#19: very small fractional quantity → costs are non-negative."""
        cm = _crypto_cost()
        pos = _make_pos(entry_price=100.0, quantity=1e-8, cm=cm)
        pnl, proceeds, _ = close_position(pos, 110.0, cm)

        assert pnl.commission >= 0
        assert pnl.slippage >= 0

    def test_scale_insufficient_cash_rejected(self):
        """#20: scaling rejected when cash insufficient."""
        positions: dict[str, PositionState] = {}
        _run_actions([Action(type="buy", symbol="TEST", quantity=10.0)], positions, cash=1100.0, prices={"TEST": 100.0})

        # Only ~100 cash left, try to add 10 more @ 100
        result = _run_actions(
            [Action(type="buy", symbol="TEST", quantity=10.0)],
            positions, cash=100.0, prices={"TEST": 100.0},
        )
        assert positions["TEST"].quantity == 10.0  # unchanged
        assert result.cash_delta == 0.0

    def test_multi_symbol_independence(self):
        """#21: scaling A doesn't affect B."""
        positions: dict[str, PositionState] = {}
        prices = {"A": 100.0, "B": 200.0}
        cm = _zero_cost()

        _run_actions([
            Action(type="buy", symbol="A", quantity=5.0),
            Action(type="buy", symbol="B", quantity=3.0),
        ], positions, prices=prices, cm=cm)

        assert positions["A"].quantity == 5.0
        assert positions["B"].quantity == 3.0

        # Scale A
        _run_actions([Action(type="buy", symbol="A", quantity=2.0)], positions, prices=prices, cm=cm)
        assert positions["A"].quantity == 7.0
        assert positions["B"].quantity == 3.0  # unchanged

    def test_equity_with_scaled_position(self):
        """#22: MTM correct after scaling."""
        from librae.core.executor import direction
        cm = _zero_cost()
        pos = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        scale_into_position(pos, _make_fill(price=120.0, quantity=5.0, cm=cm), cm)

        # MTM at price=130
        current_price = 130.0
        unrealized = cm.calc_pnl(pos.entry_price, current_price, pos.quantity) * direction(pos.side)
        notional = pos.entry_price * pos.quantity * cm.multiplier
        equity_contribution = unrealized + notional

        # Should equal current_price * total_qty (for zero-cost long)
        assert equity_contribution == pytest.approx(current_price * 15.0)

    def test_force_close_scaled_position(self):
        """#23: force-close uses avg entry price."""
        cm = _zero_cost()
        pos = _make_pos(entry_price=100.0, quantity=10.0, cm=cm)
        scale_into_position(pos, _make_fill(price=120.0, quantity=10.0, cm=cm), cm)

        pnl, _, fully_closed = close_position(pos, 130.0, cm)

        assert fully_closed
        expected_avg = (100.0 * 10.0 + 120.0 * 10.0) / 20.0  # 110
        assert pnl.gross_pnl == pytest.approx((130.0 - expected_avg) * 20.0)  # 400

    def test_long_only_regression(self):
        """#24: existing long-only pattern works identically."""
        positions: dict[str, PositionState] = {}
        cm = _zero_cost()

        # Open
        result1 = _run_actions(
            [Action(type="buy", symbol="TEST")],  # auto-size
            positions, cash=10_000.0, prices={"TEST": 100.0}, cm=cm,
        )
        assert "TEST" in positions
        assert positions["TEST"].quantity == pytest.approx(100.0)  # 10000/100

        # Hold (no action)
        result2 = _run_actions([Action(type="hold")], positions, cm=cm)
        assert result2.cash_delta == 0.0

        # Close
        result3 = _run_actions(
            [Action(type="close", symbol="TEST")],
            positions, prices={"TEST": 110.0}, cm=cm,
        )
        assert "TEST" not in positions
        assert result3.trades[0].gross_pnl == pytest.approx(1000.0)  # (110-100)*100
