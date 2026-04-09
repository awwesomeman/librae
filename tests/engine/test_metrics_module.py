"""Tests for the metrics module.

Tests compute_all() which accepts primitive sequences (equity values,
timestamps, TradePnL objects).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from librae.core.cost_model import CostModel
from librae.backtest.engine import Backtest, BacktestResult, EquitySnapshot
from librae.core.executor import TradeResult
from librae.core.strategy import Action, BaseStrategy
from librae.core.metrics import compute_all
from librae.backtest.schema import StrategyMetrics

START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2024, 2, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_trade_pnl(
    gross_pnl: float = 0.0,
    net_pnl: float = 0.0,
    commission: float = 0.0,
    slippage: float = 0.0,
    tax: float = 0.0,
    net_return: float = 0.0,
) -> SimpleNamespace:
    """Duck-typed TradePnL for tests."""
    return SimpleNamespace(
        gross_pnl=gross_pnl, net_pnl=net_pnl,
        commission=commission, slippage=slippage, tax=tax,
        gross_return=0.0, net_return=net_return,
        exit_commission=0.0, exit_slippage=0.0, exit_tax=tax,
    )


def _call_compute_all(
    equity: list[float],
    trade_pnls: list | None = None,
    exposed_periods: int | None = None,
    annualize: bool = False,
) -> StrategyMetrics:
    """Helper to call compute_all with timestamps derived from equity length."""
    timestamps = pd.date_range(START, periods=len(equity), freq="h", tz="UTC").tolist()
    return compute_all(
        equity_values=equity,
        timestamps=timestamps,
        trade_pnls=trade_pnls or [],
        total_periods=len(equity),
        annualize=annualize,
        exposed_periods=exposed_periods,
    )


class TestComputeAllEmpty:
    def test_no_equity(self) -> None:
        m = _call_compute_all([])
        assert m.trades == 0
        assert np.isclose(m.total_return, 0.0)

    def test_no_trades(self) -> None:
        m = _call_compute_all([10_000.0] * 10)
        assert m.trades == 0


class TestComputeAllMetrics:
    def test_positive_return(self) -> None:
        pnl = _make_trade_pnl(gross_pnl=100, net_pnl=100, net_return=1.0)
        m = _call_compute_all([10_000.0, 10_000.0, 10_100.0], [pnl])
        assert m.trades == 1
        assert m.total_return > 0

    def test_win_rate(self) -> None:
        pnls = [
            _make_trade_pnl(net_pnl=10, net_return=0.1),   # win
            _make_trade_pnl(net_pnl=-10, net_return=-0.1),  # loss
            _make_trade_pnl(net_pnl=5, net_return=0.05),    # win
        ]
        m = _call_compute_all([10_000.0, 10_100.0, 9_900.0, 10_050.0], pnls)
        assert np.isclose(m.win_rate, 2 / 3)

    def test_profit_factor(self) -> None:
        pnls = [
            _make_trade_pnl(net_pnl=20, net_return=0.2),
            _make_trade_pnl(net_pnl=-10, net_return=-0.1),
        ]
        m = _call_compute_all([10_000.0, 10_200.0, 10_100.0], pnls)
        assert np.isclose(m.profit_factor, 2.0, atol=1e-6)

    def test_exposure_ratio(self) -> None:
        pnl = _make_trade_pnl(net_pnl=10, net_return=0.1)
        m = _call_compute_all([10_000.0] * 20, [pnl], exposed_periods=5)
        assert np.isclose(m.exposure_ratio, 5 / 20)

    def test_cost_totals(self) -> None:
        pnl = _make_trade_pnl(
            gross_pnl=10.0, net_pnl=6.5,
            commission=2.0, slippage=1.0, tax=0.5,
            net_return=0.065,
        )
        m = _call_compute_all([10_000.0, 10_003.0, 10_006.5], [pnl])
        assert np.isclose(m.total_commission, 2.0)
        assert np.isclose(m.total_slippage, 1.0)

    def test_sharpe_is_float(self) -> None:
        pnls = [
            _make_trade_pnl(net_pnl=100, net_return=1.0),
            _make_trade_pnl(net_pnl=80, net_return=0.8),
            _make_trade_pnl(net_pnl=50, net_return=0.5),
        ]
        m = _call_compute_all([10_000.0, 10_100.0, 10_180.0, 10_230.0], pnls, annualize=True)
        assert isinstance(m.sharpe, float)

    def test_max_drawdown_negative(self) -> None:
        pnl = _make_trade_pnl(net_pnl=10, net_return=0.1)
        m = _call_compute_all([10_000.0, 10_500.0, 9_800.0, 10_200.0], [pnl])
        assert m.max_drawdown <= 0


class TestComputeAllWithEngine:
    """Integration: engine.run() → build_output() uses compute_all internally."""

    def test_engine_result_to_metrics(self) -> None:
        n = 100
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

        class BuyBar10CloseBar30(BaseStrategy):
            def on_bar(self, ctx):
                if ctx.period_index == 10 and ctx.symbol not in ctx.positions:
                    return [Action(type="long", symbol=ctx.symbol)]
                if ctx.period_index == 30 and ctx.symbol in ctx.positions:
                    return [Action(type="close", symbol=ctx.symbol)]
                return []

        cost = CostModel(
            multiplier=1.0, commission_rate=0.001,
            min_commission=0.0, slippage_ticks=0.0,
            tick_size=0.01, tax_rate=0.0,
        )
        bt = Backtest(df, BuyBar10CloseBar30(), initial_balance=10_000,
                      cost_model=cost, data_source="test")
        bt.run()
        output = bt.build_output()

        m = output.metrics
        assert isinstance(m, StrategyMetrics)
        assert m.trades >= 1
        assert isinstance(m.max_drawdown, float)
