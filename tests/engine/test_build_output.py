"""Tests for Backtest.build_output() — Step 10 of the refactor plan."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from librae.backtest.engine import Backtest
from librae.backtest.schema import BacktestOutput, StrategyMetrics
from librae.core.cost_model import CostModel
from librae.core.strategy import Action, BaseStrategy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_df(n: int = 50, symbol: str = "BTCUSDT") -> pd.DataFrame:
    close = 100.0 + np.cumsum(np.random.default_rng(42).normal(0.2, 1, n))
    dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    idx = pd.MultiIndex.from_arrays(
        [[symbol] * n, dt], names=["symbol", "datetime"],
    )
    return pd.DataFrame({
        "open": close, "high": close * 1.001,
        "low": close * 0.999, "close": close,
        "volume": np.full(n, 100.0),
    }, index=idx)


class BuyBar5CloseBar15(BaseStrategy):
    def on_bar(self, ctx):
        if ctx.bar_index == 5 and ctx.symbol not in ctx.positions:
            return [Action(type="buy", symbol=ctx.symbol)]
        if ctx.bar_index == 15 and ctx.symbol in ctx.positions:
            return [Action(type="close", symbol=ctx.symbol)]
        return []


class HoldStrategy(BaseStrategy):
    def on_bar(self, ctx):
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
        assert meta.symbol == "BTCUSDT"
        assert meta.timeframe == "H1"
        assert meta.strategy == "buy_bar5_close_bar15"

    def test_explicit_strategy_name(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), strategy_name="my_custom_name", data_source="test")
        bt.run()
        output = bt.build_output()
        assert output.run_metadata.strategy == "my_custom_name"

    def test_has_trades(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        assert len(output.trades) >= 1

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


class TestBenchmark:
    """add_benchmark → benchmark_return present; no call → None."""

    def test_no_benchmark(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        assert output.metrics.benchmark_return is None
        for pt in output.equity_curve:
            assert pt.benchmark_equity is None

    def test_with_benchmark(self) -> None:
        df = _make_df()
        benchmark_prices = df.xs("BTCUSDT", level="symbol")["close"]
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.add_benchmark(benchmark_prices)
        bt.run()
        output = bt.build_output()
        assert output.metrics.benchmark_return is not None
        assert isinstance(output.metrics.benchmark_return, float)
        # At least some equity curve points should have benchmark values
        bm_points = [p for p in output.equity_curve if p.benchmark_equity is not None]
        assert len(bm_points) > 0

    def test_annualize_false_by_default(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output()
        assert output.metrics.sharpe is None
        assert output.metrics.annual_return is None

    def test_annualize_true(self) -> None:
        df = _make_df()
        bt = Backtest(df, BuyBar5CloseBar15(), data_source="test")
        bt.run()
        output = bt.build_output(annualize=True)
        assert output.metrics.sharpe is not None
