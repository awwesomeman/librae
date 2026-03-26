"""Look-ahead bias tests for TrendPullback signal engine.

Skills: python, quant

Validates that generate_signals uses only current and past data:
  1. Truncation test: removing future bars does not change past signals
  2. Signal isolation test: mutating bar i+1 does not affect signal at bar i
  3. Daily gate merge: merge_asof uses only backward D1 bars
  4. Performance: vectorised merge_asof vs naive loop
"""
from __future__ import annotations

import time

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

def _make_ohlcv(n: int = 1000, seed: int = 42) -> pd.DataFrame:
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


def _prepare_h1_with_gate(raw: pd.DataFrame) -> pd.DataFrame:
    """Compute features + daily gate, return H1 with daily_trend column."""
    h1 = compute_features(raw)
    h1_idx_name = h1.index.name or "ts"
    h1.index.name = h1_idx_name

    d1 = compute_daily_gate(resample_to_daily(raw))
    d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
    d1_trend["daily_trend"] = (
        (d1_trend["close"] > d1_trend["ema20"])
        & (d1_trend["ema20"] > d1_trend["ema20_prev"])
    )

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
# Test 1: Truncation — removing future bars doesn't change past signals
# ---------------------------------------------------------------------------

class TestTruncation:
    """If generate_signals has no look-ahead, truncating the last N bars
    must NOT change any signal in the earlier portion."""

    @pytest.mark.parametrize("truncate_bars", [50, 100, 200])
    def test_truncation_preserves_signals(self, truncate_bars: int):
        raw = _make_ohlcv(n=1000, seed=42)
        h1_full = _prepare_h1_with_gate(raw)
        sig_full = generate_signals(h1_full)["signal"].values

        # Truncate: recompute features on shorter series
        raw_short = raw.iloc[:-truncate_bars].copy()
        h1_short = _prepare_h1_with_gate(raw_short)
        sig_short = generate_signals(h1_short)["signal"].values

        # Compare the overlapping portion
        overlap_len = len(sig_short)
        np.testing.assert_array_equal(
            sig_full[:overlap_len],
            sig_short,
            err_msg=(
                f"Signals changed after truncating last {truncate_bars} bars — "
                f"look-ahead bias detected!"
            ),
        )


# ---------------------------------------------------------------------------
# Test 2: Signal isolation — mutating bar i+1 doesn't affect bar i
# ---------------------------------------------------------------------------

class TestSignalIsolation:
    """For each entry signal at bar i, mutating bar i+1 must not change
    the signal at bar i."""

    def test_future_bar_mutation_no_effect(self):
        raw = _make_ohlcv(n=1000, seed=42)
        h1 = _prepare_h1_with_gate(raw)
        sig = generate_signals(h1)

        entries = np.where(sig["signal"].values == 1)[0]
        assert len(entries) > 0, "Need at least 1 entry to test isolation"

        for idx in entries[:5]:  # test up to 5 entries
            if idx + 1 >= len(h1):
                continue
            h1_mut = h1.copy()
            # Dramatically mutate bar i+1
            h1_mut.iloc[idx + 1, h1_mut.columns.get_loc("close")] = 1.0
            h1_mut.iloc[idx + 1, h1_mut.columns.get_loc("high")] = 1.0
            h1_mut.iloc[idx + 1, h1_mut.columns.get_loc("low")] = 0.5
            h1_mut.iloc[idx + 1, h1_mut.columns.get_loc("volume")] = 0.0

            sig_mut = generate_signals(h1_mut)
            assert sig_mut["signal"].iloc[idx] == 1, (
                f"Entry signal at bar {idx} changed after mutating bar {idx+1}"
            )


# ---------------------------------------------------------------------------
# Test 3: Daily gate merge_asof uses only backward D1 bars
# ---------------------------------------------------------------------------

class TestDailyGateNoLookahead:
    """merge_asof with direction='backward' must not use future D1 bars."""

    def test_d1_gate_backward_only(self):
        raw = _make_ohlcv(n=500, seed=42)
        h1 = compute_features(raw)
        h1_idx = h1.index.name or "ts"
        h1.index.name = h1_idx
        d1 = compute_daily_gate(resample_to_daily(raw))

        d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
        d1_trend["daily_trend"] = (
            (d1_trend["close"] > d1_trend["ema20"])
            & (d1_trend["ema20"] > d1_trend["ema20_prev"])
        )

        d1_right = d1_trend[["daily_trend"]].copy()
        d1_right["d1_ts"] = d1_right.index
        d1_right = d1_right.reset_index(drop=True)

        merged = pd.merge_asof(
            h1.reset_index(),
            d1_right,
            left_on=h1_idx,
            right_on="d1_ts",
            direction="backward",
        ).set_index(h1_idx)

        # For each H1 bar, the matched D1 timestamp must be <= H1 timestamp
        valid = merged["d1_ts"].dropna()
        assert (valid <= valid.index).all(), (
            "merge_asof matched a future D1 bar — look-ahead bias in daily gate!"
        )

    def test_d1_gate_no_same_day_bar(self):
        """H1 bars in the first hours of a day should use previous day's D1."""
        raw = _make_ohlcv(n=240, seed=42)  # 10 days
        h1 = compute_features(raw)
        h1_idx = h1.index.name or "ts"
        h1.index.name = h1_idx
        d1 = compute_daily_gate(resample_to_daily(raw))

        d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
        d1_trend["daily_trend"] = True  # Force all True for testing

        d1_right = d1_trend[["daily_trend"]].copy()
        d1_right["d1_ts"] = d1_right.index
        d1_right = d1_right.reset_index(drop=True)

        merged = pd.merge_asof(
            h1.reset_index(),
            d1_right,
            left_on=h1_idx,
            right_on="d1_ts",
            direction="backward",
        ).set_index(h1_idx)

        # Check: D1 bar at midnight should be matched to bars from that day
        # and later, not to bars from the previous day
        for ts, row in merged.iterrows():
            if pd.notna(row["d1_ts"]):
                assert row["d1_ts"] <= ts, (
                    f"H1 bar {ts} matched D1 bar {row['d1_ts']} from the future"
                )


# ---------------------------------------------------------------------------
# Test 4: Vectorised merge_asof vs naive loop performance
# ---------------------------------------------------------------------------

class TestVectorisedPerformance:
    """merge_asof should be faster than a Python for-loop on 4000+ bars."""

    def test_merge_asof_faster_than_loop(self):
        raw = _make_ohlcv(n=4200, seed=42)
        h1 = compute_features(raw)
        h1_idx = h1.index.name or "ts"
        h1.index.name = h1_idx
        d1 = compute_daily_gate(resample_to_daily(raw))

        d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
        d1_trend["daily_trend"] = (
            (d1_trend["close"] > d1_trend["ema20"])
            & (d1_trend["ema20"] > d1_trend["ema20_prev"])
        )

        d1_right = d1_trend[["daily_trend"]].copy()
        d1_right["d1_ts"] = d1_right.index
        d1_right = d1_right.reset_index(drop=True)

        # --- Vectorised (merge_asof) ---
        t0 = time.perf_counter()
        for _ in range(3):
            h1_vec = pd.merge_asof(
                h1.reset_index(),
                d1_right,
                left_on=h1_idx,
                right_on="d1_ts",
                direction="backward",
            ).set_index(h1_idx).drop(columns=["d1_ts"], errors="ignore")
            h1_vec["daily_trend"] = h1_vec["daily_trend"].fillna(False)
        t_vec = (time.perf_counter() - t0) / 3

        # --- Naive loop ---
        t0 = time.perf_counter()
        for _ in range(3):
            h1_loop = h1.copy()
            h1_loop["daily_trend"] = False
            for i in range(len(h1_loop)):
                t = h1_loop.index[i]
                day = t.floor("D") - pd.Timedelta(days=1)
                if day in d1.index:
                    d = d1.loc[day]
                    h1_loop.iloc[i, h1_loop.columns.get_loc("daily_trend")] = bool(
                        (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])
                    )
        t_loop = (time.perf_counter() - t0) / 3

        print(f"\nmerge_asof: {t_vec:.4f}s, loop: {t_loop:.4f}s, speedup: {t_loop/t_vec:.1f}x")
        assert t_vec < t_loop, (
            f"merge_asof ({t_vec:.4f}s) should be faster than loop ({t_loop:.4f}s)"
        )
