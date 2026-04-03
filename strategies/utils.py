"""Shared utilities for strategy feature engineering."""
from __future__ import annotations

import pandas as pd


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
    out[column] = out[column].fillna(fill_value)

    return out
