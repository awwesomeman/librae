"""Tests for the metrics module (QuantStats adapter).

Tests compute_all() which takes BacktestResult from the engine.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from librae.cost_model import CostModel
from librae.engine import Backtest, BacktestResult, EquitySnapshot, TradeResult
from librae.executor import BacktestExecutor
from librae.strategy import Action, BaseStrategy
from librae.metrics import compute_all
from librae.schema import StrategyMetrics

import pandas as pd

START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2024, 2, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_result(
    trades: list[TradeResult] | None = None,
    equity: list[float] | None = None,
    budget: float = 10_000.0,
) -> BacktestResult:
    """Build a BacktestResult from simplified inputs."""
    eq = equity or [10_000.0]
    snapshots = [
        EquitySnapshot(ts=START, equity=v) for v in eq
    ]
    return BacktestResult(
        trades=trades or [],
        equity_curve=snapshots,
        initial_balance=budget,
        final_equity=eq[-1],
    )


def _trade(
    entry: float, exit_: float, qty: float = 1.0, holding: int = 5,
) -> TradeResult:
    gross = (exit_ - entry) * qty
    return TradeResult(
        instrument="TEST", entry_ts=START, exit_ts=END, side="buy",
        entry_price=entry, exit_price=exit_, quantity=qty,
        gross_pnl=gross, commission=0.0, slippage=0.0, tax=0.0,
        net_pnl=gross, holding_bars=holding,
    )


class TestComputeAllEmpty:
    def test_no_equity_curve(self) -> None:
        result = BacktestResult(trades=[], equity_curve=[], initial_balance=10_000, final_equity=10_000)
        m = compute_all(result, START, END)
        assert m.trades == 0
        assert np.isclose(m.total_return, 0.0)

    def test_no_trades(self) -> None:
        result = _make_result(equity=[10_000.0] * 10)
        m = compute_all(result, START, END)
        assert m.trades == 0


class TestComputeAllMetrics:
    def test_positive_return(self) -> None:
        trades = [_trade(100, 110, qty=10)]
        equity = [10_000.0, 10_000.0, 10_100.0]
        m = compute_all(_make_result(trades, equity), START, END)
        assert m.trades == 1
        assert m.total_return > 0

    def test_win_rate(self) -> None:
        trades = [
            _trade(100, 110),   # win
            _trade(100, 90),    # loss
            _trade(100, 105),   # win
        ]
        equity = [10_000.0, 10_100.0, 9_900.0, 10_050.0]
        m = compute_all(_make_result(trades, equity), START, END)
        assert np.isclose(m.win_rate, 2 / 3)

    def test_profit_factor(self) -> None:
        trades = [
            _trade(100, 120),   # gross=20
            _trade(100, 90),    # gross=-10
        ]
        equity = [10_000.0, 10_200.0, 10_100.0]
        m = compute_all(_make_result(trades, equity), START, END)
        assert np.isclose(m.profit_factor, 2.0, atol=1e-6)

    def test_exposure_ratio(self) -> None:
        trades = [_trade(100, 110, holding=5)]
        equity = [10_000.0] * 20
        m = compute_all(_make_result(trades, equity), START, END)
        assert np.isclose(m.exposure_ratio, 5 / 20)

    def test_cost_totals(self) -> None:
        t = TradeResult(
            instrument="TEST", entry_ts=START, exit_ts=END, side="buy",
            entry_price=100, exit_price=110, quantity=1.0,
            gross_pnl=10.0, commission=2.0, slippage=1.0, tax=0.5,
            net_pnl=6.5, holding_bars=3,
        )
        equity = [10_000.0, 10_003.0, 10_006.5]
        m = compute_all(_make_result([t], equity), START, END)
        assert np.isclose(m.total_commission, 2.0)
        assert np.isclose(m.total_slippage, 1.0)

    def test_sharpe_is_float(self) -> None:
        trades = [_trade(100, 110), _trade(100, 108), _trade(100, 105)]
        equity = [10_000.0, 10_100.0, 10_180.0, 10_230.0]
        m = compute_all(_make_result(trades, equity), START, END)
        assert isinstance(m.sharpe, float)

    def test_max_drawdown_positive(self) -> None:
        equity = [10_000.0, 10_500.0, 9_800.0, 10_200.0]
        trades = [_trade(100, 110)]
        m = compute_all(_make_result(trades, equity), START, END)
        assert m.max_drawdown >= 0


class TestComputeAllWithEngine:
    """Integration: engine → metrics pipeline."""

    def test_engine_result_to_metrics(self) -> None:
        n = 100
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        prices = 100.0 + np.cumsum(np.random.default_rng(42).normal(0.5, 1, n))
        mi = pd.MultiIndex.from_arrays(
            [["TEST"] * n, idx], names=["instrument", "datetime"],
        )
        df = pd.DataFrame({
            "open": prices, "high": prices * 1.001,
            "low": prices * 0.999, "close": prices,
            "volume": np.full(n, 100.0),
        }, index=mi)

        class BuyBar10CloseBar30(BaseStrategy):
            def on_bar(self, ctx):
                if ctx.bar_index == 10 and ctx.instrument not in ctx.positions:
                    return [Action(type="buy", instrument=ctx.instrument)]
                if ctx.bar_index == 30 and ctx.instrument in ctx.positions:
                    return [Action(type="close", instrument=ctx.instrument)]
                return []

        cost = CostModel(
            multiplier=1.0, commission_rate=0.001,
            min_commission=0.0, slippage_ticks=0.0,
            tick_size=0.01, transaction_tax=0.0,
        )
        bt = Backtest(df, BuyBar10CloseBar30(), initial_balance=10_000,
                      executor=BacktestExecutor(cost))
        result = bt.run()
        m = compute_all(result, idx[0].to_pydatetime(), idx[-1].to_pydatetime())

        assert isinstance(m, StrategyMetrics)
        assert m.trades >= 1
        assert isinstance(m.sharpe, float)
        assert isinstance(m.max_drawdown, float)
