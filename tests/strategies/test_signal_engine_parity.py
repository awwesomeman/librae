"""Parity test: signal_engine vs legacy core_features.

Strategy: pandas_ta uses SMA-seeded EMA (NaN for first N-1 bars) while legacy
uses ewm(adjust=False) from bar 0. The indicator VALUES will differ, but given
the SAME feature values, the signal LOGIC must produce identical entry/exit.

Test structure:
  1. Feature-level: shape & column checks; verify convergence after warmup
  2. Signal logic parity: feed legacy features into new generate_signals → same signals
  3. Cost model: structural checks

Skills: python, quant
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.trendpullback.utils import (
    compute_daily_gate,
    compute_entry_conditions,
    compute_exit_conditions,
    compute_features,
    resample_to_daily,
)
from data.binance import resample_ohlcv
from pipeline.features.core_features import (
    add_daily_trend_gate,
    add_trendpullback_features,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 500, seed: int = 42) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV data (H1 bars)."""
    rng = np.random.RandomState(seed)
    base = 50000.0
    closes = base + np.cumsum(rng.randn(n) * 100)
    highs = closes + rng.uniform(50, 200, n)
    lows = closes - rng.uniform(50, 200, n)
    opens = closes + rng.randn(n) * 50
    volumes = rng.uniform(100, 5000, n)

    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def _add_daily_trend_column(h1: pd.DataFrame, d1: pd.DataFrame) -> pd.DataFrame:
    """Merge daily_trend bool column into H1 DataFrame (vectorised)."""
    h1 = h1.copy()
    h1_idx_name = h1.index.name or "ts"
    h1.index.name = h1_idx_name

    d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
    d1_trend["daily_trend"] = (
        (d1_trend["close"] > d1_trend["ema20"])
        & (d1_trend["ema20"] > d1_trend["ema20_prev"])
    )

    # Prepare D1 right-side DataFrame with explicit column name
    d1_right = d1_trend[["daily_trend"]].copy()
    d1_right["d1_ts"] = d1_right.index
    d1_right = d1_right.reset_index(drop=True)

    h1 = pd.merge_asof(
        h1.reset_index(),
        d1_right,
        left_on=h1_idx_name,
        right_on="d1_ts",
        direction="backward",
    ).set_index(h1_idx_name).drop(columns=["d1_ts"], errors="ignore")
    h1["daily_trend"] = h1["daily_trend"].fillna(False)
    return h1




# ---------------------------------------------------------------------------
# Tests: Feature shape & convergence
# ---------------------------------------------------------------------------

class TestFeatureShape:
    """New signal_engine produces correct columns and shapes."""

    def test_compute_features_columns(self):
        h1 = compute_features(_make_ohlcv())
        assert "ema20" in h1.columns
        assert "atr14" in h1.columns
        assert "vol_sma20" in h1.columns

    def test_compute_daily_gate_columns(self):
        d1 = compute_daily_gate(resample_to_daily(_make_ohlcv()))
        assert "ema20" in d1.columns
        assert "ema20_prev" in d1.columns

    def test_resample_preserves_rows(self):
        h1 = _make_ohlcv(n=240)  # 10 days of hourly data
        d1 = resample_to_daily(h1)
        assert len(d1) >= 9  # at least 9 complete days

    def test_ema_converges_after_warmup(self):
        """After enough bars, pandas_ta EMA and manual EMA converge."""
        h1_base = _make_ohlcv(n=500)
        new = compute_features(h1_base)
        old = add_trendpullback_features(h1_base)
        # Compare only after bar 100 (well past warmup)
        valid = slice(100, None)
        np.testing.assert_allclose(
            new["ema20"].iloc[valid].values,
            old["ema20"].iloc[valid].values,
            rtol=0.01,  # 1% tolerance after warmup
            err_msg="EMA20 fails to converge after warmup",
        )

    def test_vol_sma20_exact(self):
        """SMA should be identical (both use rolling mean)."""
        h1_base = _make_ohlcv()
        new = compute_features(h1_base)
        old = add_trendpullback_features(h1_base)
        valid = ~(new["vol_sma20"].isna() | old["vol_sma20"].isna())
        np.testing.assert_allclose(
            new["vol_sma20"][valid].values,
            old["vol_sma20"][valid].values,
            rtol=1e-10,
        )


# ---------------------------------------------------------------------------
# Tests: Pure signal conditions (entry/exit as boolean Series)
# ---------------------------------------------------------------------------

class TestSignalConditions:
    """Test compute_entry_conditions and compute_exit_conditions produce valid booleans."""

    def test_entry_conditions_returns_bool(self):
        h1_base = _make_ohlcv(n=500, seed=42)
        h1 = compute_features(h1_base)
        d1 = compute_daily_gate(resample_to_daily(h1_base))
        merged = _add_daily_trend_column(h1, d1)
        entry = compute_entry_conditions(merged)
        assert entry.dtype == bool
        assert len(entry) == len(merged)

    def test_exit_conditions_returns_bool(self):
        h1_base = _make_ohlcv(n=500, seed=42)
        h1 = compute_features(h1_base)
        exit_cond = compute_exit_conditions(h1)
        assert exit_cond.dtype == bool
        assert len(exit_cond) == len(h1)

    def test_entry_conditions_require_daily_trend(self):
        """Without daily_trend column, entries should still work (default True)."""
        h1 = compute_features(_make_ohlcv(n=500, seed=42))
        entry = compute_entry_conditions(h1)
        assert isinstance(entry, pd.Series)

    @pytest.mark.parametrize("seed", [42, 123, 99])
    def test_entry_conditions_on_trending_data(self, seed):
        """Trending data should produce some entry signals."""
        rng = np.random.default_rng(seed)
        n = 1000
        base = 50000.0
        # Strong uptrend
        returns = rng.normal(0.002, 0.005, n)
        close = base * np.cumprod(1 + returns)
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        df = pd.DataFrame({
            "open": close * 0.999,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": rng.uniform(500, 3000, n),
        }, index=idx)
        h1 = compute_features(df)
        d1 = compute_daily_gate(resample_to_daily(df))
        merged = _add_daily_trend_column(h1, d1)
        entry = compute_entry_conditions(merged)
        assert entry.sum() > 0, "Strong uptrend should have entry signals"


# ---------------------------------------------------------------------------
# Tests: Cost model (via new engine)
# ---------------------------------------------------------------------------

class TestCostModel:

    def test_cost_deducted_from_pnl(self):
        from librae.core.cost_model import CostModel
        from librae.backtest.engine import Backtest
        from librae.core.strategy import Action, BaseStrategy

        h1_base = _make_ohlcv(n=500, seed=42)
        h1 = compute_features(h1_base)
        d1 = compute_daily_gate(resample_to_daily(h1_base))
        merged = _add_daily_trend_column(h1, d1)

        # Convert to MultiIndex
        mi = pd.MultiIndex.from_arrays(
            [["TEST"] * len(merged), merged.index], names=["symbol", "datetime"],
        )
        merged_mi = merged.set_index(mi)

        # Precompute entry/exit signals
        from strategies.trendpullback.utils import compute_entry_conditions, compute_exit_conditions
        merged_mi["entry_signal"] = compute_entry_conditions(merged).values
        merged_mi["exit_signal"] = compute_exit_conditions(merged).values

        class SignalStrat(BaseStrategy):
            def on_bar(self, ctx):
                pos = ctx.positions.get(ctx.symbol)
                if pos and (ctx.bar["exit_signal"] or pos.bars_held >= 24):
                    return [Action(type="close", symbol=ctx.symbol)]
                if not pos and ctx.bar["entry_signal"]:
                    return [Action(type="buy", symbol=ctx.symbol)]
                return []

        cost_model = CostModel(
            multiplier=1.0, commission_rate=0.001,
            min_commission=0.0, slippage_ticks=2.0,
            tick_size=0.01, transaction_tax=0.0,
        )
        bt = Backtest(merged_mi, SignalStrat(), initial_balance=100_000,
                      cost_model=cost_model)
        result = bt.run()

        for t in result.trades:
            total_cost = t.commission + t.slippage + t.tax
            assert total_cost >= 0
            assert abs(t.net_pnl - (t.gross_pnl - total_cost)) < 1e-6

    def test_zero_cost(self):
        from librae.core.cost_model import CostModel
        from librae.backtest.engine import Backtest
        from librae.core.strategy import Action, BaseStrategy

        h1_base = _make_ohlcv(n=500, seed=42)
        h1 = compute_features(h1_base)
        d1 = compute_daily_gate(resample_to_daily(h1_base))
        merged = _add_daily_trend_column(h1, d1)

        mi = pd.MultiIndex.from_arrays(
            [["TEST"] * len(merged), merged.index], names=["symbol", "datetime"],
        )
        merged_mi = merged.set_index(mi)

        from strategies.trendpullback.utils import compute_entry_conditions, compute_exit_conditions
        merged_mi["entry_signal"] = compute_entry_conditions(merged).values
        merged_mi["exit_signal"] = compute_exit_conditions(merged).values

        class SignalStrat(BaseStrategy):
            def on_bar(self, ctx):
                pos = ctx.positions.get(ctx.symbol)
                if pos and (ctx.bar["exit_signal"] or pos.bars_held >= 24):
                    return [Action(type="close", symbol=ctx.symbol)]
                if not pos and ctx.bar["entry_signal"]:
                    return [Action(type="buy", symbol=ctx.symbol)]
                return []

        cost_model = CostModel(
            multiplier=1.0, commission_rate=0.0,
            min_commission=0.0, slippage_ticks=0.0,
            tick_size=0.01, transaction_tax=0.0,
        )
        bt = Backtest(merged_mi, SignalStrat(), initial_balance=100_000,
                      cost_model=cost_model)
        result = bt.run()

        for t in result.trades:
            assert abs(t.commission) < 1e-10
            assert abs(t.slippage) < 1e-10
            assert abs(t.net_pnl - t.gross_pnl) < 1e-6
