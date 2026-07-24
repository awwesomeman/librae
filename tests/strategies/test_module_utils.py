"""Tests for strategies.module.utils — generic (non-factor-specific) helpers
shared across strategy families.

merge_htf_column's own contract (backward merge_asof direction, fillna
default, output dtype) is tested once here — every strategy that merges a
higher-timeframe gate onto a lower timeframe (trendpullback, mtf_trend_rsi,
...) calls this same function, so its mechanics don't need re-deriving in
each strategy's own test file. What each strategy's own look-ahead test
should still cover is narrower: did *that strategy's* gate-computation
function remember to shift(1) before calling this — a caller mistake this
function can't detect on its own, since it has no way to know whether the
series it was handed is already-shifted or not.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.module.utils import merge_htf_column, split_is_val_oos


def _htf_series(n_days: int = 5) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=n_days, freq="D", tz="UTC")
    return pd.Series([True, False, True, True, False][:n_days], index=idx)


def _detail_index(n_days: int = 5) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n_days * 24, freq="h", tz="UTC", name="ts")


def test_backward_merge_no_forward_peek():
    """Each H1 bar only ever sees an HTF bar at or before its own timestamp."""
    htf = _htf_series()
    detail = pd.DataFrame({"x": np.arange(len(_detail_index()))}, index=_detail_index())

    merged = merge_htf_column(detail, htf, column="g", fill_value=False)

    for day_idx, day_start in enumerate(htf.index):
        day_end = day_start + pd.Timedelta(days=1)
        day_bars = merged.loc[(merged.index >= day_start) & (merged.index < day_end)]
        assert (day_bars["g"] == htf.iloc[day_idx]).all()


def test_fillna_default_before_first_htf_bar():
    htf = _htf_series()
    early_index = pd.date_range("2024-12-30", periods=24, freq="h", tz="UTC", name="ts")
    detail = pd.DataFrame({"x": np.arange(len(early_index))}, index=early_index)

    merged = merge_htf_column(detail, htf, column="g", fill_value=False)

    assert not merged["g"].isna().any()
    assert (merged["g"] == False).all()  # noqa: E712


def test_output_dtype_is_bool_even_after_shift():
    """htf_series.shift(1) (the caller's usual no-lookahead fix) upcasts a
    bool Series to object (NaN in the first slot) — merge_htf_column must
    not leave that as object dtype, or later bitwise ops on the merged
    column trip numpy's "~ on bool" deprecation warning."""
    htf = _htf_series().shift(1)
    detail = pd.DataFrame({"x": np.arange(len(_detail_index()))}, index=_detail_index())

    merged = merge_htf_column(detail, htf, column="g", fill_value=False)

    assert merged["g"].dtype == bool


def test_split_is_val_oos_boundaries_are_disjoint_and_exhaustive():
    """Regression test: label-based .loc[a:b] is inclusive on both ends, so
    naively chaining df.loc[:x], df.loc[x:y], df.loc[y:z] leaked the x and y
    boundary bars into two splits at once — a real train/val/test overlap
    bug. Each boundary bar must belong to exactly one split, and every row
    in the input must land in exactly one output split."""
    idx = pd.date_range("2024-01-01", periods=10, freq="D")
    df = pd.DataFrame({"x": range(10)}, index=idx)

    splits = split_is_val_oos(df, "2024-01-04", "2024-01-07", "2024-01-10")

    all_ts = pd.concat([splits["IS-Train"], splits["IS-Val"], splits["OOS"]]).index
    assert len(all_ts) == len(df)
    assert all_ts.is_unique
    assert pd.Timestamp("2024-01-04") in splits["IS-Train"].index
    assert pd.Timestamp("2024-01-04") not in splits["IS-Val"].index
    assert pd.Timestamp("2024-01-07") in splits["IS-Val"].index
    assert pd.Timestamp("2024-01-07") not in splits["OOS"].index
