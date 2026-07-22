"""Shared composable primitives for ``strategies/factors/library.py``.

Thin wraps around pandas — kept intentionally small. Add a new operator only
when >=2 factors in ``library.py`` would otherwise duplicate the same rolling
logic; don't pre-build operators no factor uses yet.
"""
from __future__ import annotations

import pandas as pd


def ts_mean(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ts_std(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).std()


def zscore(s: pd.Series, n: int) -> pd.Series:
    return (s - ts_mean(s, n)) / ts_std(s, n)


def delta(s: pd.Series, n: int) -> pd.Series:
    return s.diff(n)


def momentum(s: pd.Series, n: int) -> pd.Series:
    """n-period pct change: ``s / s.shift(n) - 1``."""
    return s / s.shift(n) - 1.0


def rolling_corr(a: pd.Series, b: pd.Series, n: int) -> pd.Series:
    return a.rolling(n).corr(b)
