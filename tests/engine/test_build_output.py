"""Tests for Backtest.build_output() — Step 10 of the refactor plan."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.backtest.schema import BacktestOutput, StrategyMetrics
from librae.core.cost_model import CostModel
from librae.core.strategy import OrderIntent, Strategy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_df(n: int = 50, symbol: str = "BTCUSDT") -> pd.DataFrame:
    close = 100.0 + np.cumsum(np.random.default_rng(42).normal(0.2, 1, n))
    dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    idx = pd.MultiIndex.from_arrays(
        [[symbol] * n, dt],
        names=["symbol", "datetime"],
    )
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": np.full(n, 100.0),
        },
        index=idx,
    )


class BuyBar5CloseBar15(Strategy):
    def on_bar(self, ctx):
        if ctx.period_index == 5 and ctx.symbol not in ctx.positions:
            return [OrderIntent(action="long", symbol=ctx.symbol)]
        if ctx.period_index == 15 and ctx.symbol in ctx.positions:
            return [OrderIntent(action="close", symbol=ctx.symbol)]
        return []


class HoldStrategy(Strategy):
    def on_bar(self, ctx):
        return []


class BuyBar5AndHold(Strategy):
    def on_bar(self, ctx):
        if ctx.period_index == 5 and ctx.symbol not in ctx.positions:
            return [OrderIntent(action="long", symbol=ctx.symbol)]
        return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildOutputValid:
    """build_output() produces a valid BacktestOutput."""

    def test_returns_backtest_output(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        assert isinstance(output, BacktestOutput)

    def test_validate_passes(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        output.validate()

    def test_has_run_metadata(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        meta = output.run_metadata
        assert meta.run_id == bt.run_id
        assert meta.symbols == ("BTCUSDT",)
        assert meta.timeframe == "H1"
        assert meta.strategy == "buy_bar5_close_bar15"

    def test_explicit_strategy_name(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), strategy_name="my_custom_name", data_source="test")
        bt.run()
        output = bt.build_output()
        assert output.run_metadata.strategy == "my_custom_name"

    def test_has_close_events(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        close_events = [e for e in output.order_events if e.event_type == "close"]
        assert len(close_events) >= 1

    def test_has_equity_curve(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        assert len(output.equity_curve) == 50

    def test_metrics_accessible(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        assert isinstance(output.metrics, StrategyMetrics)
        assert isinstance(bt.metrics, StrategyMetrics)

    def test_terminal_liquidation_costs_are_in_equity_and_metrics(self) -> None:
        df = _make_df()
        cost = CostModel(
            multiplier=1.0,
            commission_rate=0.001,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )
        bt = Backtest(
            df,
            BuyBar5AndHold(),
            initial_balance=10_000.0,
            cost_model=cost,
            data_source="test",
            record_position_snapshots=True,
        )

        result = bt.run()
        output = bt.build_output()

        assert result.equity_curve[-1].equity == pytest.approx(result.final_equity)
        assert output.equity_curve[-1].equity == pytest.approx(result.final_equity)
        assert output.metrics.total_return == pytest.approx(
            result.final_equity / result.initial_cash - 1.0
        )
        final_ts = result.equity_curve[-1].ts
        assert all(snapshot.ts != final_ts for snapshot in result.position_snapshots)
        assert all(snapshot.ts != final_ts for snapshot in output.position_snapshots)


class TestBuildOutputBeforeRun:
    """build_output() before run() raises RuntimeError."""

    def test_raises_runtime_error(self) -> None:
        df = _make_df()
        bt = Backtest(df, HoldStrategy(), data_source="test")
        with pytest.raises(RuntimeError, match="Call run"):
            bt.build_output()

    def test_metrics_before_build_raises(self) -> None:
        df = _make_df()
        bt = Backtest(df, HoldStrategy(), data_source="test")
        bt.run()
        with pytest.raises(RuntimeError, match="build_output"):
            _ = bt.metrics


class TestPeriodMetrics:
    """Engine output keeps only generic, nonannualized period metrics."""

    def test_build_output_contains_nonannualized_period_metrics(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        assert output.metrics.mean_period_return is not None
        assert output.metrics.period_volatility is not None
