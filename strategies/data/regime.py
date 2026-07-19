"""Regime-overlay columns for factor slicing — categorical labels (fed to
factrix.by_slice), not continuous factors (fed to factrix.evaluate).

``vol_regime`` is computed from an asset's own OHLCV, so it applies to any
symbol. ``fng_regime`` (Fear & Greed Index) and ``dxy_trend`` (US Dollar
Index) are crypto-market-wide macro/sentiment series with no TW-futures
analogue — callers should pass ``is_crypto=False`` for non-crypto symbols to
get the same neutral defaults (fng=50/"weak_dxy") a strategy's own fallback
path should already be using, so strategy code runs unchanged regardless of
which regimes are actually present.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta


def compute_vol_regime(df: pd.DataFrame, window: int = 14, baseline: int = 120) -> pd.Series:
    """"high_vol" / "low_vol" — today's ATR ratio vs its own rolling baseline.
    Asset-agnostic: only needs high/low/close columns."""
    atr = ta.atr(df["high"], df["low"], df["close"], length=window)
    atr_ratio = atr / df["close"]
    baseline_avg = atr_ratio.rolling(baseline).mean()
    return pd.Series(np.where(atr_ratio > baseline_avg, "high_vol", "low_vol"), index=df.index)


def fetch_historical_fng(limit: int = 1000) -> pd.DataFrame:
    """Fetch historical Fear & Greed Index from alternative.me. Returns an
    empty frame (caller falls back to neutral) if the request fails —
    external API availability shouldn't crash the research pipeline."""
    import httpx

    url = f"https://api.alternative.me/fng/?limit={limit}&format=json"
    try:
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        records = [
            {"date": pd.to_datetime(int(item["timestamp"]), unit="s").date(), "fng_value": int(item["value"])}
            for item in data
        ]
        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame(columns=["date", "fng_value"])


def fetch_dxy_trend(start: str, end: str) -> pd.DataFrame:
    """Daily US Dollar Index (DX-Y.NYB) trend label — no crypto-exchange or
    Shioaji equivalent, so this is the one place this repo reaches for
    yfinance directly. Requires the ``research`` extra (``pip install -e '.[research]'``)."""
    import yfinance as yf

    dxy_raw = yf.download("DX-Y.NYB", start=start, end=end, progress=False)
    if isinstance(dxy_raw.columns, pd.MultiIndex):
        dxy_raw.columns = dxy_raw.columns.get_level_values(0)
    dxy_raw = dxy_raw.reset_index().rename(columns={"Date": "date"})
    dxy_raw["date"] = pd.to_datetime(dxy_raw["date"]).dt.date
    dxy_raw["dxy_sma_20"] = dxy_raw["Close"].rolling(20).mean()
    dxy_raw["dxy_trend"] = np.where(dxy_raw["Close"] > dxy_raw["dxy_sma_20"], "strong_dxy", "weak_dxy")
    return dxy_raw[["date", "dxy_trend"]]


def attach_regime_columns(ohlcv: pd.DataFrame, is_crypto: bool, start: str, end: str) -> pd.DataFrame:
    """Attach vol_regime (always) and fng_regime/dxy_trend (crypto only,
    neutral default otherwise) to an OHLCV DataFrame shaped like
    ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp`` column)."""
    df = ohlcv.copy()
    df["vol_regime"] = compute_vol_regime(df)

    if is_crypto:
        df["date_only"] = pd.to_datetime(df["timestamp"]).dt.date

        dxy = fetch_dxy_trend(start, end)
        df = df.merge(dxy, left_on="date_only", right_on="date", how="left", suffixes=("", "_dxy"))
        df["dxy_trend"] = df["dxy_trend"].ffill().fillna("weak_dxy")

        fng = fetch_historical_fng(limit=1000)
        if not fng.empty:
            df = df.merge(fng, left_on="date_only", right_on="date", how="left", suffixes=("", "_fng"))
            df["fng_value"] = df["fng_value"].ffill().fillna(50)
        else:
            df["fng_value"] = 50.0
        df["fng_regime"] = np.where(df["fng_value"] < 40, "fear", "greed")
        df = df.drop(columns=["date_only"])
    else:
        df["dxy_trend"] = "weak_dxy"
        df["fng_value"] = 50.0
        df["fng_regime"] = "greed"

    return df
