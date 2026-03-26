"""Tests for max_hold_bars enforcement in signal engine.

Verifies that the TrendPullback engine emits exit signals after max_hold_bars,
so the runner never holds indefinitely.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_lab.signal_engine.trendpullback import (
    DEFAULT_PARAMS,
    generate_signals,
)


def _make_bullish_df(n_bars: int = 40) -> pd.DataFrame:
    """Create a synthetic H1 DataFrame that triggers entry on bar 1
    and stays bullish throughout (price always above EMA, volume high).

    Entry conditions (from generate_signals):
      1. daily_trend = True
      2. abs(low - ema20) <= pullback_factor * atr14  (near EMA)
      3. close > open  (bullish bar)
      4. close > prev high  (breakout)
      5. volume >= vol_threshold * vol_sma20

    Columns: open, high, low, close, volume, ema20, atr14, vol_sma20, daily_trend
    """
    np.random.seed(42)
    base_price = 50000.0
    # Rising prices with large enough steps so close[i] > high[i-1]
    step = 50.0
    closes = base_price + np.arange(n_bars) * step
    opens = closes - 20.0  # bullish: close > open
    highs = closes + 5.0   # small high above close
    lows = closes - 30.0   # low near ema for pullback

    # close[i] = base + i*50, prev_high = base + (i-1)*50 + 5
    # close[i] - prev_high = 50 - 5 = 45 > 0 ✓ (breakout holds for all i>=1)

    # EMA20 = low (so abs(low - ema20) = 0 <= pullback * atr)
    ema20 = lows.copy()
    atr14 = np.full(n_bars, 200.0)  # large ATR
    vol_sma20 = np.full(n_bars, 1000.0)
    volume = np.full(n_bars, 2000.0)  # above vol threshold

    ts = pd.date_range("2025-01-01", periods=n_bars, freq="1h")
    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volume,
            "ema20": ema20,
            "atr14": atr14,
            "vol_sma20": vol_sma20,
            "daily_trend": True,
        },
        index=ts,
    )
    df.index.name = "ts"
    return df


class TestMaxHoldBars:
    """Verify max_hold_bars forces exit after the configured limit."""

    def test_exit_emitted_at_max_hold(self):
        """Engine must emit signal=-1 within max_hold_bars after entry."""
        df = _make_bullish_df(40)
        sig_df = generate_signals(df, params={"max_hold_bars": 24})
        signals = sig_df["signal"].values

        # Find first entry
        entry_idx = int(np.argmax(signals == 1))
        assert entry_idx > 0, "Expected an entry signal"

        # Find first exit after entry
        exit_indices = np.where(signals[entry_idx + 1 :] == -1)[0]
        assert len(exit_indices) > 0, "Expected an exit signal after entry"

        first_exit_offset = exit_indices[0] + 1  # offset from entry
        assert first_exit_offset <= 24, (
            f"Exit should occur within 24 bars of entry, got {first_exit_offset}"
        )

    def test_no_infinite_hold(self):
        """With enough bars, every entry gets an exit within max_hold_bars.
        The last open position may remain open (runner force-closes it)."""
        df = _make_bullish_df(55)  # enough for 2 full cycles
        sig_df = generate_signals(df, params={"max_hold_bars": 24})
        signals = sig_df["signal"].values

        entries = np.where(signals == 1)[0]
        exits = np.where(signals == -1)[0]

        assert len(entries) > 0, "Should have at least one entry"
        # With enough data, each entry (except possibly the last) gets an exit
        assert len(exits) >= len(entries) - 1, (
            f"Each entry needs an exit (last may be open): entries={len(entries)}, exits={len(exits)}"
        )
        # Verify holding duration: each exit should be <= max_hold_bars from its entry
        for idx in range(min(len(entries), len(exits))):
            hold = exits[idx] - entries[idx]
            assert hold <= 24, f"Trade {idx}: held {hold} bars > 24"

    def test_reentry_after_max_hold_exit(self):
        """After a max-hold exit, the engine should allow re-entry on the next
        qualifying bar (not stay permanently flat)."""
        # Use 60 bars so there's room for entry → hold 24 → exit → re-entry
        df = _make_bullish_df(60)
        sig_df = generate_signals(df, params={"max_hold_bars": 24})
        signals = sig_df["signal"].values

        entries = np.where(signals == 1)[0]
        exits = np.where(signals == -1)[0]

        assert len(entries) >= 2, (
            f"Expected at least 2 entries (entry → exit → re-entry), got {len(entries)}"
        )
        # Second entry must come after first exit
        assert entries[1] > exits[0], "Re-entry must come after the first exit"

    def test_custom_max_hold_bars(self):
        """Custom max_hold_bars=10 should force exit within 10 bars."""
        df = _make_bullish_df(30)
        sig_df = generate_signals(df, params={"max_hold_bars": 10})
        signals = sig_df["signal"].values

        entry_idx = int(np.argmax(signals == 1))
        assert entry_idx > 0, "Expected an entry signal"

        exit_indices = np.where(signals[entry_idx + 1 :] == -1)[0]
        assert len(exit_indices) > 0, "Expected an exit signal"

        first_exit_offset = exit_indices[0] + 1
        assert first_exit_offset <= 10, (
            f"Exit should occur within 10 bars, got {first_exit_offset}"
        )
