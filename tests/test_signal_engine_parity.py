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

from quant_lab.signal_engine.trendpullback import (
    compute_daily_gate,
    compute_features,
    generate_signals,
    resample_to_daily,
)
from scripts.etl.core_features import (
    add_daily_trend_gate,
    add_trendpullback_features,
    resample_ohlcv,
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
    """Merge daily_trend bool column into H1 DataFrame."""
    h1 = h1.copy()
    h1["daily_trend"] = False
    for i in range(len(h1)):
        t = h1.index[i]
        day = t.floor("D") - pd.Timedelta(days=1)
        if day in d1.index:
            d = d1.loc[day]
            h1.iloc[i, h1.columns.get_loc("daily_trend")] = bool(
                (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])
            )
    return h1


def _legacy_signals(h1: pd.DataFrame, d1: pd.DataFrame) -> np.ndarray:
    """Reproduce old inline signal logic (operates on already-computed features)."""
    PULL = 0.3
    MAX_HOLD = 24
    n = len(h1)
    signals = np.zeros(n, dtype=np.int8)
    in_position = False
    bars_held = 0

    for i in range(1, n):
        t = h1.index[i]
        cur = h1.iloc[i]
        prev = h1.iloc[i - 1]

        if in_position:
            bars_held += 1
            if cur["close"] < cur["ema20"] or bars_held >= MAX_HOLD:
                signals[i] = -1
                in_position = False
                bars_held = 0
            continue

        if i >= n - 1:
            continue

        day = t.floor("D") - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]
        trend = (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])
        if not trend:
            continue

        near = abs(cur["low"] - cur["ema20"]) <= PULL * cur["atr14"]
        if not near:
            continue

        bullish = (cur["close"] > cur["open"]) and (cur["close"] > prev["high"])
        if not bullish:
            continue

        vol_ok = (
            (cur["volume"] >= 0.9 * cur["vol_sma20"])
            if not np.isnan(cur["vol_sma20"])
            else False
        )
        if not (vol_ok and cur["atr14"] > 0):
            continue

        signals[i] = 1
        in_position = True
        bars_held = 0

    return signals


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
# Tests: Signal logic parity (same features → same signals)
# ---------------------------------------------------------------------------

class TestSignalLogicParity:
    """Given the SAME features, generate_signals must produce identical signals
    as the legacy inline logic."""

    @pytest.mark.parametrize("n,seed", [(1000, 42), (800, 123), (600, 99)])
    def test_same_features_same_signals(self, n: int, seed: int):
        h1_base = _make_ohlcv(n=n, seed=seed)

        # Use legacy features for both paths
        h1_legacy = add_trendpullback_features(h1_base)
        d1_legacy = add_daily_trend_gate(resample_ohlcv(h1_base, "1D"))

        # Legacy signals
        old_signals = _legacy_signals(h1_legacy, d1_legacy)

        # New engine signals (using same legacy features + daily_trend column)
        h1_for_engine = _add_daily_trend_column(h1_legacy, d1_legacy)
        new_signals = generate_signals(h1_for_engine)["signal"].values

        new_entries = np.where(new_signals == 1)[0]
        old_entries = np.where(old_signals == 1)[0]
        new_exits = np.where(new_signals == -1)[0]
        old_exits = np.where(old_signals == -1)[0]

        np.testing.assert_array_equal(
            new_entries, old_entries,
            err_msg=f"Entry mismatch (seed={seed}): new={new_entries}, old={old_entries}",
        )
        np.testing.assert_array_equal(
            new_exits, old_exits,
            err_msg=f"Exit mismatch (seed={seed}): new={new_exits}, old={old_exits}",
        )

    def test_signal_engine_end_to_end(self):
        """Full pipeline with pandas_ta features produces valid signals."""
        h1_base = _make_ohlcv(n=1000, seed=42)
        h1 = compute_features(h1_base)
        d1 = compute_daily_gate(resample_to_daily(h1_base))
        h1_with_gate = _add_daily_trend_column(h1, d1)
        sig = generate_signals(h1_with_gate)

        assert "signal" in sig.columns
        assert set(sig["signal"].unique()).issubset({-1, 0, 1})
        assert len(sig) == len(h1_base)

        # Entries and exits should pair up
        entries = (sig["signal"] == 1).sum()
        exits = (sig["signal"] == -1).sum()
        assert abs(entries - exits) <= 1, "Entry/exit count mismatch > 1"


# ---------------------------------------------------------------------------
# Tests: Cost model
# ---------------------------------------------------------------------------

class TestCostModel:

    def test_cost_deducted_from_pnl(self):
        h1_base = _make_ohlcv(n=500, seed=42)
        h1 = compute_features(h1_base)
        d1 = compute_daily_gate(resample_to_daily(h1_base))

        from scripts.run_backtest_lumibot_btc import _run_vectorised_backtest
        trade_log, _ = _run_vectorised_backtest(
            h1, d1, budget=100_000, warmup_days=7,
            commission_bps=10.0, slippage_bps=5.0,
        )

        for t in trade_log:
            assert "net_pnl" in t
            assert "gross_pnl" in t
            assert "cost" in t
            assert t["cost"] >= 0
            assert abs(t["net_pnl"] - (t["gross_pnl"] - t["cost"])) < 1e-6

    def test_zero_cost(self):
        h1_base = _make_ohlcv(n=500, seed=42)
        h1 = compute_features(h1_base)
        d1 = compute_daily_gate(resample_to_daily(h1_base))

        from scripts.run_backtest_lumibot_btc import _run_vectorised_backtest
        trade_log, _ = _run_vectorised_backtest(
            h1, d1, budget=100_000, warmup_days=7,
            commission_bps=0.0, slippage_bps=0.0,
        )

        for t in trade_log:
            assert abs(t["cost"]) < 1e-10
            assert abs(t["net_pnl"] - t["gross_pnl"]) < 1e-6
