"""TrendPullback M5 utils — M30 trend gate + M5 entry/exit signals.

Same indicator logic as trendpullback, different timeframes:
- M5 bars for features (EMA, ATR) and entry/exit signals
- M30 bars for trend gate (replaces D1)
"""
from __future__ import annotations

import pandas as pd

from data.binance import fetch_ohlcv, resample_ohlcv
from strategies.trendpullback.utils import (
    compute_daily_gate,
    compute_entry_conditions,
    compute_exit_conditions,
    compute_features,
)


def _merge_m30_gate(m5: pd.DataFrame, m30: pd.DataFrame) -> pd.DataFrame:
    """Merge M30 trend gate into M5 via merge_asof (no look-ahead)."""
    m5_feat = m5.copy()
    idx_name = m5_feat.index.name or "ts"

    m30_trend = m30[["close", "ema20", "ema20_prev"]].copy()
    m30_trend["daily_trend"] = (
        (m30_trend["close"] > m30_trend["ema20"])
        & (m30_trend["ema20"] > m30_trend["ema20_prev"])
    )

    m30_right = m30_trend[["daily_trend"]].copy()
    m30_right["m30_ts"] = m30_right.index
    m30_right = m30_right.reset_index(drop=True)

    m5_feat = pd.merge_asof(
        m5_feat.reset_index(),
        m30_right,
        left_on=idx_name,
        right_on="m30_ts",
        direction="backward",
    ).set_index(idx_name).drop(columns=["m30_ts"], errors="ignore")
    m5_feat["daily_trend"] = m5_feat["daily_trend"].fillna(False)

    return m5_feat


def prepare_signals(m5_base: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add features + signals to an M5 OHLCV DataFrame.

    M30 trend gate + M5 entry/exit signals.
    Handles short datasets (< 20 M30 bars) by skipping trend gate.
    """
    m5 = compute_features(m5_base, params)
    m30 = resample_ohlcv(m5_base, "30min")
    if len(m30) >= 20:
        m30 = compute_daily_gate(m30, params)
        m5 = _merge_m30_gate(m5, m30)
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
    raw = fetch_ohlcv(symbol=symbol, interval="5m", months=months)
    m5_base = raw.set_index("timestamp")
    m5_base.index.name = "ts"

    m5 = prepare_signals(m5_base, params)

    mi = pd.MultiIndex.from_arrays(
        [[symbol] * len(m5), m5.index], names=["symbol", "datetime"],
    )
    return m5.set_index(mi)
