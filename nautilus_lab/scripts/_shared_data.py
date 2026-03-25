"""Shared data preparation for runner scripts."""
from __future__ import annotations

import pandas as pd

from nautilus_lab.backtest.sample_data import generate_btc_h1_ohlcv
from nautilus_lab.strategies.trendpullback_btc import (
    add_daily_gate,
    add_h1_features,
    resample_ohlcv,
)


def prepare_sample_data(n_bars: int = 2160) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Generate deterministic BTC H1 sample data for offline backtest/sim.

    Returns (m1, h1, d1) where m1 is raw OHLCV, h1 has H1 features, d1 has daily gate.
    """
    df = generate_btc_h1_ohlcv(n_bars=n_bars)
    df = df.set_index("timestamp")
    m1 = df.copy()
    h1 = add_h1_features(df)
    d1 = add_daily_gate(resample_ohlcv(df, "1D"))
    return m1, h1, d1
