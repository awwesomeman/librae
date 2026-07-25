"""Tests for librae.core.executor.calc_trade_pnl — shared PnL calculation."""

from __future__ import annotations

import pytest
from librae.core.cost_model import CostModel
from librae.core.executor import TradePnL, calc_trade_pnl


def _crypto_cost_model() -> CostModel:
    """Typical crypto cost model: 0.1% commission, no tax."""
    return CostModel(
        multiplier=1.0,
        commission_rate=0.001,
        min_commission=0.0,
        slippage_ticks=0.0,
        tick_size=0.01,
        tax_rate=0.0,
    )


class TestCalcTradePnl:
    """calc_trade_pnl produces correct PnL breakdown."""

    def test_long_profit(self) -> None:
        cm = _crypto_cost_model()
        pnl = calc_trade_pnl(
            entry_price=100.0,
            exit_price=110.0,
            quantity=10.0,
            side="long",
            cost_model=cm,
            entry_commission=cm.calc_commission(100.0, 10.0),
            entry_slippage=0.0,
        )
        assert isinstance(pnl, TradePnL)
        assert pnl.gross_pnl == pytest.approx(100.0)  # (110-100)*10
        assert pnl.commission == pytest.approx(2.1)  # entry 1.0 + exit 1.1
        assert pnl.net_pnl == pytest.approx(100.0 - 2.1)
        assert pnl.gross_return > 0
        assert pnl.net_return > 0

    def test_long_loss(self) -> None:
        cm = _crypto_cost_model()
        pnl = calc_trade_pnl(
            entry_price=100.0,
            exit_price=90.0,
            quantity=10.0,
            side="long",
            cost_model=cm,
            entry_commission=cm.calc_commission(100.0, 10.0),
            entry_slippage=0.0,
        )
        assert pnl.gross_pnl == pytest.approx(-100.0)
        assert pnl.net_pnl < pnl.gross_pnl  # costs make it worse

    def test_short_profit(self) -> None:
        cm = _crypto_cost_model()
        pnl = calc_trade_pnl(
            entry_price=100.0,
            exit_price=90.0,
            quantity=10.0,
            side="short",
            cost_model=cm,
            entry_commission=cm.calc_commission(100.0, 10.0),
            entry_slippage=0.0,
        )
        assert pnl.gross_pnl == pytest.approx(100.0)  # short profit when price drops

    def test_zero_cost_model(self) -> None:
        cm = CostModel.zero()
        pnl = calc_trade_pnl(
            entry_price=100.0,
            exit_price=110.0,
            quantity=1.0,
            side="long",
            cost_model=cm,
            entry_commission=0.0,
            entry_slippage=0.0,
        )
        assert pnl.gross_pnl == pnl.net_pnl  # no costs
        assert pnl.commission == 0.0
        assert pnl.slippage == 0.0
        assert pnl.tax == 0.0

    def test_with_tax(self) -> None:
        cm = CostModel(
            multiplier=1.0,
            commission_rate=0.001,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.003,  # 0.3% sell tax
        )
        pnl = calc_trade_pnl(
            entry_price=100.0,
            exit_price=110.0,
            quantity=10.0,
            side="long",
            cost_model=cm,
            entry_commission=cm.calc_commission(100.0, 10.0),
            entry_slippage=0.0,
        )
        assert pnl.tax > 0  # tax applied on exit
        assert pnl.net_pnl < pnl.gross_pnl - pnl.commission - pnl.slippage  # tax reduces further

    def test_consistent_with_backtest_close(self) -> None:
        """calc_trade_pnl result matches close_position + build_trade_result."""
        from librae.core.executor import build_trade_result, close_position
        from librae.core.strategy import PositionState

        cm = _crypto_cost_model()

        pos = PositionState(
            symbol="TEST",
            side="long",
            entry_price=100.0,
            quantity=10.0,
            entry_at=None,
            periods_held=5,
            entry_commission=cm.calc_commission(100.0, 10.0),
            entry_slippage=cm.calc_slippage(10.0),
            entry_tax=cm.calc_tax(100.0, 10.0),
            total_entry_cost=100.0 * 10.0,
        )
        pnl_close, _, _ = close_position(pos, 110.0, cm)
        trade = build_trade_result(pos, None, 110.0, pos.quantity, pnl_close)

        pnl = calc_trade_pnl(
            entry_price=100.0,
            exit_price=110.0,
            quantity=10.0,
            side="long",
            cost_model=cm,
            entry_commission=pos.entry_commission,
            entry_slippage=pos.entry_slippage,
        )

        assert trade.gross_pnl == pytest.approx(pnl.gross_pnl)
        assert trade.net_pnl == pytest.approx(pnl.net_pnl)
        assert trade.commission == pytest.approx(pnl.commission)
        assert trade.slippage == pytest.approx(pnl.slippage)
        assert trade.gross_return == pytest.approx(pnl.gross_return)
        assert trade.net_return == pytest.approx(pnl.net_return)

    def test_futures_multiplier(self) -> None:
        """Multiplier is correctly applied (futures instruments)."""
        cm = CostModel(
            multiplier=200.0,  # futures
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )
        pnl = calc_trade_pnl(
            entry_price=100.0,
            exit_price=101.0,
            quantity=1.0,
            side="long",
            cost_model=cm,
            entry_commission=0.0,
            entry_slippage=0.0,
        )
        assert pnl.gross_pnl == pytest.approx(200.0)  # 1 point * 200 multiplier
