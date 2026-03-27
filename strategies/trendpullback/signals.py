"""TrendPullback signal engine — pure functions, no I/O, no position tracking.

All indicator computations use pandas_ta_classic (EMA, ATR, SMA).
Functions are deterministic: same input → same output.

Entry/exit conditions are pure boolean Series — no in_position or bars_held.
Position management belongs in the Strategy layer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta


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
    x = pd.DataFrame()
    x["open"] = df["open"].resample("1D").first()
    x["high"] = df["high"].resample("1D").max()
    x["low"] = df["low"].resample("1D").min()
    x["close"] = df["close"].resample("1D").last()
    x["volume"] = df["volume"].resample("1D").sum()
    return x.dropna()


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


