"""Look-ahead bias test for MTF Trend RSI's daily momentum gate.

merge_htf_column's own merge mechanics (backward direction, fillna, dtype)
are already covered generically by ``tests/strategies/test_module_utils.py``
— every strategy that merges an HTF gate calls that same shared function, so
those checks don't need re-deriving per strategy. What's specific to *this*
family, and worth testing here, is narrower: did
``strategies/experiments/mtf_trend_rsi/utils.py::merge_daily_gate`` remember to
``shift(1)`` its gate before calling the shared merge — a caller mistake the
shared function has no way to detect on its own. That exact mistake has now
shown up twice in this repo (see this family's report.md "跟原始研究的落差"
section) and doesn't fail loudly — it silently inflates backtest
performance instead.

Skills: python, quant
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.experiments.mtf_trend_rsi.utils import (
    compute_daily_momentum_gate,
    merge_daily_gate,
    prepare_signals,
)
from strategies.module.data.utils import resample_ohlcv

N_BARS = 720  # 30 days — enough D1 bars for the trend_lookback=10 gate to be non-NaN


def _make_ohlcv(n: int = N_BARS, seed: int = 42) -> pd.DataFrame:
    """Deterministic synthetic OHLCV (H1 bars)."""
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


class TestDailyGateNoLeak:
    """H1 bars must only see completed (past) daily mom_1D_10 gate values."""

    def test_d1_trend_change_not_visible_same_day(self):
        """When mom_1D_10's sign flips on day N, H1 bars on day N itself —
        before day N's own session has closed — must NOT see it; only bars
        from day N+1 onward may. This is the exact bug class this test
        guards: a naive backward merge_asof against an un-shifted gate leaks
        day N's own still-forming value onto day N's own H1 bars."""
        h1_base = _make_ohlcv()
        d1 = resample_ohlcv(h1_base, "1D")
        merged = merge_daily_gate(h1_base, d1)

        gate = compute_daily_momentum_gate(d1)
        changes = gate.ne(gate.shift(1))
        change_days = gate.index[changes & (gate.index > gate.index[1])]
        if len(change_days) == 0:
            pytest.skip("No gate flip in test data")

        flip_day = change_days[0]
        new_value = gate.loc[flip_day]
        next_day = flip_day + pd.Timedelta(days=1)

        flip_day_h1 = merged.loc[(merged.index >= flip_day) & (merged.index < next_day)]
        if len(flip_day_h1) > 0:
            assert (flip_day_h1["daily_trend_up"] != new_value).all(), (
                f"H1 bars on {flip_day.date()} itself saw that day's own "
                "not-yet-closed gate flip — same-day look-ahead."
            )

        next_day_h1 = merged.loc[(merged.index >= next_day) & (merged.index < next_day + pd.Timedelta(days=1))]
        if len(next_day_h1) > 0:
            assert (next_day_h1["daily_trend_up"] == new_value).all(), (
                f"H1 bars on {next_day.date()} should see {flip_day.date()}'s now-completed gate flip"
            )


class TestSignalNoFutureLeak:
    """Truncating future bars must not change signals for past bars."""

    def test_signals_bulk_match_on_truncation(self):
        h1_base = _make_ohlcv()
        full = prepare_signals(h1_base)

        for cut in [504, 600, 720]:
            truncated = prepare_signals(h1_base.iloc[:cut])
            shared = truncated.index
            for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
                pd.testing.assert_series_equal(
                    full.loc[shared, col],
                    truncated[col],
                    check_names=False,
                    obj=f"{col} mismatch at cut={cut}",
                )
