"""Tests for strategies.experiments.kdj_oversold.utils — KDJ oversold signal."""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.experiments.kdj_oversold.utils import prepare_signals


def _make_ohlcv(n: int = 60, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = pd.Series(100 + np.cumsum(rng.normal(size=n)))
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open": close, "high": close + 1, "low": close - 1, "close": close,
        "volume": 100.0,
    }, index=pd.Index(ts, name="ts"))


def test_adds_kdj_and_entry_signal_columns():
    df = prepare_signals(_make_ohlcv())
    assert {"kdj_k", "kdj_d", "kdj_j", "entry_signal"} <= set(df.columns)


def test_entry_signal_is_binary_and_matches_j_threshold():
    df = prepare_signals(_make_ohlcv())
    assert set(df["entry_signal"].dropna().unique()) <= {0, 1}
    valid = df.dropna(subset=["kdj_j"])
    assert (valid["entry_signal"] == (valid["kdj_j"] < 20).astype(int)).all()


def test_custom_threshold_changes_signal_count():
    df = _make_ohlcv()
    loose = prepare_signals(df, {"j_threshold": 80})
    strict = prepare_signals(df, {"j_threshold": 5})
    assert loose["entry_signal"].sum() >= strict["entry_signal"].sum()
