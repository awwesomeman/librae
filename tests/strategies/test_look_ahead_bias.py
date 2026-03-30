"""Look-ahead bias tests for TrendPullback signal pipeline + engine.

Verifies that:
1. Signal[i] depends only on bars[0..i] (no future data)
2. D1→H1 merge uses only completed daily bars (backward join)
3. Engine executes at bar[i] close, not bar[i+1] open

Skills: python, quant
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from librae.cost_model import CostModel
from librae.engine import Backtest
from librae.executor import BacktestExecutor
from librae.strategy import Action, BaseStrategy
from strategies.trendpullback.utils import (
    _merge_daily_gate,
    compute_daily_gate,
    compute_entry_conditions,
    compute_exit_conditions,
    compute_features,
    resample_to_daily,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# WHY: n=720 (30 days) ensures D1 has ≥20 bars for EMA20 to be non-NaN
N_BARS = 720


def _make_ohlcv(n: int = N_BARS, seed: int = 42) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV data (H1 bars)."""
    rng = np.random.RandomState(seed)
    base = 50000.0
    closes = base + np.cumsum(rng.randn(n) * 100)
    highs = closes + rng.uniform(50, 200, n)
    lows = closes - rng.uniform(50, 200, n)
    opens = closes + rng.randn(n) * 50
    volumes = rng.uniform(100, 5000, n)

    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC", name="ts")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def _prepare_signals(h1_base: pd.DataFrame) -> pd.DataFrame:
    """Run full feature + signal pipeline on H1 data.

    Requires ≥480 H1 bars (20 days) so D1 EMA20 has enough data.
    """
    h1 = compute_features(h1_base)
    d1 = resample_to_daily(h1_base)
    if len(d1) < 20:
        # WHY: pandas_ta EMA returns None when rows < period, causing TypeError
        # in _merge_daily_gate. Skip daily gate when data is too short.
        h1["daily_trend"] = True
    else:
        d1 = compute_daily_gate(d1)
        h1 = _merge_daily_gate(h1, d1)
    h1["entry_signal"] = compute_entry_conditions(h1).values
    h1["exit_signal"] = compute_exit_conditions(h1).values
    return h1


def _zero_cost_executor() -> BacktestExecutor:
    return BacktestExecutor(CostModel(
        multiplier=1.0, commission_rate=0.0, min_commission=0.0,
        slippage_ticks=0.0, tick_size=0.01, transaction_tax=0.0,
    ))


# ---------------------------------------------------------------------------
# Test 1: Signal[i] depends only on bars[0..i]
# ---------------------------------------------------------------------------

class TestSignalNoFutureLeak:
    """Truncating future bars must not change signals for past bars."""

    def test_entry_signal_stable_on_truncation(self):
        """entry_signal[i] must be identical whether computed on bars[0..N] or bars[0..i]."""
        h1_base = _make_ohlcv()
        full = _prepare_signals(h1_base)

        entry_bars = full.index[full["entry_signal"]]
        if len(entry_bars) == 0:
            pytest.skip("No entry signals in test data")

        for ts in entry_bars[:5]:
            loc = full.index.get_loc(ts)
            truncated = _prepare_signals(h1_base.iloc[: loc + 1])
            assert truncated["entry_signal"].iloc[-1] == True, (
                f"entry_signal at {ts} changed when future bars removed"
            )

    def test_exit_signal_stable_on_truncation(self):
        """exit_signal[i] must be identical whether computed on bars[0..N] or bars[0..i]."""
        h1_base = _make_ohlcv()
        full = _prepare_signals(h1_base)

        exit_bars = full.index[full["exit_signal"]]
        if len(exit_bars) == 0:
            pytest.skip("No exit signals in test data")

        for ts in exit_bars[:5]:
            loc = full.index.get_loc(ts)
            truncated = _prepare_signals(h1_base.iloc[: loc + 1])
            assert truncated["exit_signal"].iloc[-1] == True, (
                f"exit_signal at {ts} changed when future bars removed"
            )

    def test_signals_bulk_match_on_truncation(self):
        """All signals up to bar T must match between full and truncated runs."""
        h1_base = _make_ohlcv()
        full = _prepare_signals(h1_base)

        # WHY: cut must be ≥480 (20 days) to have enough D1 bars for EMA20
        for cut in [504, 600, 720]:
            truncated = _prepare_signals(h1_base.iloc[:cut])
            shared = truncated.index
            pd.testing.assert_series_equal(
                full.loc[shared, "entry_signal"],
                truncated["entry_signal"],
                check_names=False,
                obj=f"entry_signal mismatch at cut={cut}",
            )
            pd.testing.assert_series_equal(
                full.loc[shared, "exit_signal"],
                truncated["exit_signal"],
                check_names=False,
                obj=f"exit_signal mismatch at cut={cut}",
            )


# ---------------------------------------------------------------------------
# Test 2: D1 → H1 merge has no future daily leak
# ---------------------------------------------------------------------------

class TestDailyMergeNoLeak:
    """H1 bars must only see completed (past) daily bars."""

    def test_backward_merge_direction(self):
        """Verify merge_asof uses backward direction (no forward peek)."""
        h1_base = _make_ohlcv()
        h1 = compute_features(h1_base)
        d1 = compute_daily_gate(resample_to_daily(h1_base))

        merged = _merge_daily_gate(h1, d1)

        assert not merged["daily_trend"].isna().any()

        # First few H1 bars (before first complete D1) should be False
        first_d1_ts = d1.index[0]
        early_bars = merged.loc[merged.index < first_d1_ts]
        if len(early_bars) > 0:
            assert (early_bars["daily_trend"] == False).all(), (
                "H1 bars before first D1 close should have daily_trend=False"
            )

    def test_d1_trend_change_not_visible_before_close(self):
        """When D1 trend flips on day N, H1 bars on day N-1 must not see it."""
        h1_base = _make_ohlcv()
        h1 = compute_features(h1_base)
        d1 = compute_daily_gate(resample_to_daily(h1_base))
        merged = _merge_daily_gate(h1, d1)

        # Find a day where daily_trend changes
        d1_trend = (
            (d1["close"] > d1["ema20"])
            & (d1["ema20"] > d1["ema20_prev"])
        ).fillna(False)

        changes = d1_trend.ne(d1_trend.shift(1))
        change_days = d1_trend.index[changes & (d1_trend.index > d1_trend.index[1])]
        if len(change_days) == 0:
            pytest.skip("No trend change in test data")

        flip_day = change_days[0]
        new_trend_value = d1_trend.loc[flip_day]

        # H1 bars on the day BEFORE the flip must NOT have the new trend value
        prev_day = flip_day - pd.Timedelta(days=1)
        prev_day_h1 = merged.loc[
            (merged.index >= prev_day) & (merged.index < flip_day)
        ]
        if len(prev_day_h1) > 0:
            assert (prev_day_h1["daily_trend"] != new_trend_value).all(), (
                f"H1 bars on {prev_day.date()} leaked trend from {flip_day.date()}"
            )


# ---------------------------------------------------------------------------
# Test 3: Engine execution timing
# ---------------------------------------------------------------------------

class _BuyBar5Strategy(BaseStrategy):
    """Deterministic strategy: buy at bar 5, close at bar 10."""

    def on_bar(self, ctx) -> list[Action]:
        pos = ctx.positions.get(ctx.instrument)
        if pos and ctx.bar_index >= 10:
            return [Action(type="close", instrument=ctx.instrument)]
        if not pos and ctx.bar_index == 5:
            return [Action(type="buy", instrument=ctx.instrument)]
        return []


class TestEngineExecutionTiming:
    """Engine must execute at the correct bar's close price."""

    def _make_simple_df(self, n: int = 20) -> pd.DataFrame:
        """Create a simple DataFrame with known prices."""
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        prices = np.arange(100.0, 100.0 + n, 1.0)
        df = pd.DataFrame({
            "open": prices - 0.5,
            "high": prices + 1.0,
            "low": prices - 1.0,
            "close": prices,
            "volume": np.full(n, 1000.0),
            "entry_signal": False,
            "exit_signal": False,
        }, index=idx)
        mi = pd.MultiIndex.from_arrays(
            [["TEST"] * n, idx], names=["instrument", "datetime"],
        )
        return df.set_index(mi)

    def test_entry_at_close_of_signal_bar(self):
        """Trade entry price must equal close[i] where signal fired."""
        df = self._make_simple_df()
        bt = Backtest(df, _BuyBar5Strategy(), initial_balance=100_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.entry_price == 105.0, (
            f"Entry should be at bar 5 close (105.0), got {trade.entry_price}"
        )

    def test_exit_at_close_of_signal_bar(self):
        """Trade exit price must equal close[j] where exit signal fired."""
        df = self._make_simple_df()
        bt = Backtest(df, _BuyBar5Strategy(), initial_balance=100_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_price == 110.0, (
            f"Exit should be at bar 10 close (110.0), got {trade.exit_price}"
        )

    def test_pnl_consistent_with_entry_exit_prices(self):
        """gross_pnl must equal (exit - entry) * quantity * direction."""
        df = self._make_simple_df()
        bt = Backtest(df, _BuyBar5Strategy(), initial_balance=100_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        trade = result.trades[0]
        expected_pnl = (trade.exit_price - trade.entry_price) * trade.quantity
        assert abs(trade.gross_pnl - expected_pnl) < 1e-6

    def test_holding_bars_count(self):
        """holding_bars must equal number of bars from entry to exit."""
        df = self._make_simple_df()
        bt = Backtest(df, _BuyBar5Strategy(), initial_balance=100_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        trade = result.trades[0]
        assert trade.holding_bars == 5, (
            f"Expected 5 holding bars, got {trade.holding_bars}"
        )

    def test_equity_curve_no_future_pnl(self):
        """Equity should not jump before the trade entry."""
        df = self._make_simple_df()
        bt = Backtest(df, _BuyBar5Strategy(), initial_balance=100_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        eq = result.equity_curve
        for i in range(5):
            assert eq[i].equity == 100_000.0, (
                f"Equity at bar {i} should be 100000, got {eq[i].equity}"
            )
