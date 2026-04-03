"""TrendPullback utils — data fetching, ETL, feature engineering, signal computation.

All indicator computations use pandas_ta_classic (EMA, ATR, SMA).
Pure functions: same input → same output.

Entry/exit conditions are pure boolean Series — no position tracking.
Position management belongs in the Strategy layer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from data.binance import resample_ohlcv
from data.ohlcv import get_ohlcv


# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------
DEFAULT_PARAMS: dict = {
    "ema_period": 20,
    "atr_period": 14,
    "vol_sma_period": 20,
    "pullback_factor": 0.3,
    "max_hold_bars": 24,
    "vol_threshold": 0.9,
}


# ---------------------------------------------------------------------------
# Feature computation (pure)
# ---------------------------------------------------------------------------

def compute_features(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add technical features to an OHLCV DataFrame (H1).

    Required columns: open, high, low, close, volume.
    Returns a *copy* with extra columns: ema20, atr14, vol_sma20.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()

    out["ema20"] = ta.ema(out["close"], length=p["ema_period"])
    out["atr14"] = ta.atr(
        out["high"], out["low"], out["close"], length=p["atr_period"],
    )
    out["vol_sma20"] = ta.sma(out["volume"], length=p["vol_sma_period"])

    return out


def compute_daily_gate(df_1d: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add daily trend gate columns to a D1 OHLCV DataFrame.

    Returns a *copy* with: ema20, ema20_prev.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df_1d.copy()
    out["ema20"] = ta.ema(out["close"], length=p["ema_period"])
    out["ema20_prev"] = out["ema20"].shift(1)
    return out


def resample_to_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Resample H1 (or sub-daily) OHLCV to D1. Pure function."""
    return resample_ohlcv(df, "1D")


# ---------------------------------------------------------------------------
# Signal conditions (pure boolean Series — no position tracking)
# ---------------------------------------------------------------------------

def compute_entry_conditions(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """Return boolean Series: True where all entry conditions are met.

    Conditions:
    1. daily_trend gate is True (pre-merged into df)
    2. Pullback near EMA20: |low - ema20| <= pullback_factor * atr14
    3. Bullish bar: close > open AND close > prev high
    4. Volume filter: volume >= vol_threshold * vol_sma20
    5. ATR > 0 (valid volatility)

    No position tracking. No bars_held. Just market conditions.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    daily_trend = df.get("daily_trend", pd.Series(True, index=df.index))
    near_ema = (df["low"] - df["ema20"]).abs() <= p["pullback_factor"] * df["atr14"]
    bullish = (df["close"] > df["open"]) & (df["close"] > df["high"].shift(1))
    vol_ok = df["volume"] >= p["vol_threshold"] * df["vol_sma20"]
    atr_valid = df["atr14"] > 0

    entry = daily_trend & near_ema & bullish & vol_ok & atr_valid

    # NaN safety: first few bars won't have indicators
    return entry.fillna(False)


def compute_exit_conditions(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """Return boolean Series: True where exit condition is met.

    Condition: close < ema20 (trend broken).
    max_hold_bars is NOT checked here — that's the Strategy's job.
    """
    return (df["close"] < df["ema20"]).fillna(False)


# ---------------------------------------------------------------------------
# Data pipeline: fetch → features → signals → MultiIndex
# ---------------------------------------------------------------------------


def prepare_signals(h1_base: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add features + signals to an H1 OHLCV DataFrame.

    Expects h1_base with DatetimeIndex named 'ts' and OHLCV columns.
    Returns the same DataFrame with added indicator and signal columns.
    Handles short datasets (< 20 D1 bars) by skipping daily gate.
    """
    h1 = compute_features(h1_base, params)
    d1 = resample_to_daily(h1_base)
    if len(d1) >= 20:
        d1 = compute_daily_gate(d1, params)
        h1 = _merge_daily_gate(h1, d1)
    else:
        h1["daily_trend"] = True
    h1["entry_signal"] = compute_entry_conditions(h1, params).values
    h1["exit_signal"] = compute_exit_conditions(h1, params).values
    return h1


def fetch_and_prepare(
    symbol: str = "BTCUSDT",
    months: int = 6,
    params: dict | None = None,
) -> pd.DataFrame:
    """End-to-end data preparation: fetch OHLCV → features → signals → MultiIndex.

    Returns a MultiIndex DataFrame (symbol, datetime) ready for Backtest.
    """
    h1_raw = get_ohlcv(symbol=symbol, interval="1h", months=months)
    h1_base = h1_raw.set_index("timestamp")
    h1_base.index.name = "ts"

    # Features
    h1 = compute_features(h1_base, params)

    # Daily gate
    d1 = compute_daily_gate(resample_to_daily(h1_base), params)
    h1 = _merge_daily_gate(h1, d1)

    # Signals — .values strips index to avoid alignment mismatch after merge_asof
    h1["entry_signal"] = compute_entry_conditions(h1, params).values
    h1["exit_signal"] = compute_exit_conditions(h1, params).values

    # MultiIndex
    mi = pd.MultiIndex.from_arrays(
        [[symbol] * len(h1), h1.index], names=["symbol", "datetime"],
    )
    return h1.set_index(mi)


def _merge_daily_gate(h1: pd.DataFrame, d1: pd.DataFrame) -> pd.DataFrame:
    """Merge D1 daily_trend bool into H1 via merge_asof (no look-ahead)."""
    h1_feat = h1.copy()
    idx_name = h1_feat.index.name or "ts"

    d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
    d1_trend["daily_trend"] = (
        (d1_trend["close"] > d1_trend["ema20"])
        & (d1_trend["ema20"] > d1_trend["ema20_prev"])
    )

    d1_right = d1_trend[["daily_trend"]].copy()
    d1_right["d1_ts"] = d1_right.index
    d1_right = d1_right.reset_index(drop=True)

    h1_feat = pd.merge_asof(
        h1_feat.reset_index(),
        d1_right,
        left_on=idx_name,
        right_on="d1_ts",
        direction="backward",
    ).set_index(idx_name).drop(columns=["d1_ts"], errors="ignore")
    h1_feat["daily_trend"] = h1_feat["daily_trend"].fillna(False)

    return h1_feat
