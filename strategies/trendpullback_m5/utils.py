"""TrendPullback M5 utils — M30 trend gate + M5 entry/exit signals.

Same indicator logic as trendpullback, different timeframes:
- M5 bars for features (EMA, ATR) and entry/exit signals
- M30 bars for trend gate (replaces D1)
"""
from __future__ import annotations

import pandas as pd

from data.binance import resample_ohlcv
from data.ohlcv import get_ohlcv
from strategies.trendpullback.utils import (
    compute_daily_gate,
    compute_entry_conditions,
    compute_exit_conditions,
    compute_features,
    merge_trend_gate,
)


def prepare_signals(m5_base: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add features + signals to an M5 OHLCV DataFrame.

    M30 trend gate + M5 entry/exit signals.
    Handles short datasets (< 20 M30 bars) by skipping trend gate.
    """
    m5 = compute_features(m5_base, params)
    m30 = resample_ohlcv(m5_base, "30min")
    if len(m30) >= 20:
        m30 = compute_daily_gate(m30, params)
        m5 = merge_trend_gate(m5, m30)
    else:
        m5["daily_trend"] = True
    m5["entry_signal"] = compute_entry_conditions(m5, params).values
    m5["exit_signal"] = compute_exit_conditions(m5, params).values
    return m5


def fetch_and_prepare(
    symbol: str = "BTCUSDT",
    months: int = 1,
    params: dict | None = None,
) -> pd.DataFrame:
    """Fetch M5 OHLCV → features → signals → MultiIndex for backtest."""
    raw = get_ohlcv(symbol=symbol, interval="5m", months=months)
    m5_base = raw.set_index("timestamp")
    m5_base.index.name = "ts"

    m5 = prepare_signals(m5_base, params)

    mi = pd.MultiIndex.from_arrays(
        [[symbol] * len(m5), m5.index], names=["symbol", "datetime"],
    )
    return m5.set_index(mi)
