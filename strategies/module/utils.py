"""Shared utilities for strategy feature engineering."""
from __future__ import annotations

import pandas as pd


def split_is_val_oos(
    df: pd.DataFrame, is_train_end: str, is_val_end: str, oos_end: str,
) -> dict[str, pd.DataFrame]:
    """Slice a DatetimeIndex-ed DataFrame into IS-Train/IS-Val/OOS, per
    strategies/RESEARCH_METHODOLOGY.md's 2 — split order is
    fixed; OOS is only ever read after IS-Train/IS-Val decisions are
    already final. Generic (not factor-specific) — any research script
    slicing a sample into these three windows can use this."""
    return {
        "IS-Train": df.loc[:is_train_end],
        "IS-Val": df.loc[is_train_end:is_val_end],
        "OOS": df.loc[is_val_end:oos_end],
    }


def merge_htf_column(
    detail: pd.DataFrame,
    htf_series: pd.Series,
    column: str = "trend_gate",
    fill_value: bool = False,
) -> pd.DataFrame:
    """Merge a higher-TF Series into lower-TF via merge_asof (no look-ahead).

    Parameters
    ----------
    detail : DataFrame with DatetimeIndex (lower timeframe, e.g. H1 / M5).
    htf_series : Series with DatetimeIndex (higher timeframe, e.g. D1 / M30).
    column : target column name in the output.
    fill_value : value for bars before the first HTF bar.
    """
    idx_name = detail.index.name or "ts"

    right = pd.DataFrame({column: htf_series, "_htf_ts": htf_series.index})
    right = right.reset_index(drop=True)

    out = pd.merge_asof(
        detail.reset_index(),
        right,
        left_on=idx_name,
        right_on="_htf_ts",
        direction="backward",
    ).set_index(idx_name).drop(columns=["_htf_ts"], errors="ignore")
    # WHY astype(bool): htf_series.shift(1) (the caller's usual no-lookahead
    # fix) upcasts a bool Series to object (NaN in the first slot), and
    # fillna() alone doesn't cast it back — leaves an object-dtype column of
    # Python bool instances, which trips numpy's "~ on bool" deprecation
    # warning on any later bitwise negation (e.g. ``~df["daily_trend_up"]``).
    out[column] = out[column].fillna(fill_value).astype(bool)

    return out
