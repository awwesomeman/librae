"""TrendPullBack strategy definition for MXFR1 (台指期小型).

Pure strategy definition: parameters + feature engineering.
No execution logic — use librae.engine for backtesting.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TrendPullBackParams:
    pullback_atr_ratio: float = 0.30
    breakout_lookback_bars: int = 5
    ema_span: int = 20
    time_stop_bars: int = 6
    round_trip_cost_points: float = 2.0


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["vol_sma20"] = out["volume"].rolling(20).mean()
    return out


def resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    agg = df.resample(freq).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"},
    )
    return agg.dropna()
