"""TrendPullback signal engine — pure functions, no I/O.

All indicator computations use pandas_ta_classic (EMA, ATR, SMA).
Functions are deterministic: same input → same output.

Skills: python, quant
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
        out["high"], out["low"], out["close"], length=p["atr_period"]
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
# Signal generation (pure)
# ---------------------------------------------------------------------------

def generate_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Generate entry/exit signals for TrendPullback strategy.

    Parameters
    ----------
    df : pd.DataFrame
        H1 OHLCV with features already computed (ema20, atr14, vol_sma20).
        Must also contain a ``daily_trend`` column (bool) indicating whether
        the daily gate is bullish for each bar's date.
    params : dict, optional
        Override default parameters.

    Returns
    -------
    pd.DataFrame
        A single-column DataFrame (index = df.index) with column ``signal``:
        - 1  = entry (long)
        - -1 = exit
        - 0  = hold / no action
    """
    p = {**DEFAULT_PARAMS, **(params or {})}

    n = len(df)
    signals = np.zeros(n, dtype=np.int8)

    pull = p["pullback_factor"]
    max_hold = p["max_hold_bars"]
    vol_thresh = p["vol_threshold"]

    in_position = False
    bars_held = 0

    for i in range(1, n):
        cur = df.iloc[i]
        prev = df.iloc[i - 1]

        # --- Exit ---
        if in_position:
            bars_held += 1
            if cur["close"] < cur["ema20"] or bars_held >= max_hold:
                signals[i] = -1
                in_position = False
                bars_held = 0
            continue

        # --- Entry conditions ---
        # Skip last bar
        if i >= n - 1:
            continue

        # Daily gate
        if not cur.get("daily_trend", False):
            continue

        # Pullback near EMA20
        near = abs(cur["low"] - cur["ema20"]) <= pull * cur["atr14"]
        if not near:
            continue

        # Bullish bar
        bullish = (cur["close"] > cur["open"]) and (cur["close"] > prev["high"])
        if not bullish:
            continue

        # Volume filter
        vol_ok = (
            (cur["volume"] >= vol_thresh * cur["vol_sma20"])
            if not np.isnan(cur["vol_sma20"])
            else False
        )
        if not (vol_ok and cur["atr14"] > 0):
            continue

        # Entry
        signals[i] = 1
        in_position = True
        bars_held = 0

    return pd.DataFrame({"signal": signals}, index=df.index)
