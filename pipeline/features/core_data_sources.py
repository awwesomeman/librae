#!/usr/bin/env python3
"""Data source adapters for reusable ETL pipelines.

All Binance fetching delegates to data.ohlcv.get_ohlcv() which
handles DB caching, gap-fill, and retry logic.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd


def normalize_ohlcv(df: pd.DataFrame, ts_col: str = "ts") -> pd.DataFrame:
    """Normalize OHLCV DataFrame to standard format (DatetimeIndex named 'ts')."""
    out = df.copy()
    if ts_col != "ts":
        out = out.rename(columns={ts_col: "ts"})
    keep = ["ts", "open", "high", "low", "close", "volume"]
    out = out[keep]
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna().set_index("ts").sort_index()


def fetch_binance_spot_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    **kwargs,
) -> pd.DataFrame:
    """Fetch Binance spot klines via unified get_ohlcv()."""
    from data.ohlcv import get_ohlcv

    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    df = get_ohlcv(symbol=symbol, interval=interval, start=start_dt, end=end_dt, source="binance_spot")
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if "timestamp" in df.columns:
        df = df.rename(columns={"timestamp": "ts"})
    return normalize_ohlcv(df, ts_col="ts")


def fetch_binance_futures_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    **kwargs,
) -> pd.DataFrame:
    """Fetch Binance futures klines via unified get_ohlcv()."""
    from data.ohlcv import get_ohlcv

    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    df = get_ohlcv(symbol=symbol, interval=interval, start=start_dt, end=end_dt, source="binance_futures")
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if "timestamp" in df.columns:
        df = df.rename(columns={"timestamp": "ts"})
    return normalize_ohlcv(df, ts_col="ts")


def fetch_shioaji_mxfr1_1m(start: str, end: str, simulation: bool = True) -> pd.DataFrame:
    """Fetch Shioaji MXF futures 1-min klines."""
    import shioaji as sj

    api_key = os.getenv("SINO_API_KEY")
    secret_key = os.getenv("SINO_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("missing SINO_API_KEY/SINO_SECRET_KEY")

    api = sj.Shioaji(simulation=simulation)
    api.login(api_key=api_key, secret_key=secret_key)
    kb = api.kbars(api.Contracts.Futures.MXF.MXFR1, start=start, end=end)
    api.logout()

    raw = pd.DataFrame({**kb})
    if raw.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    raw["ts"] = pd.to_datetime(raw["ts"], utc=True)
    raw = raw.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    return normalize_ohlcv(raw, ts_col="ts")


def now_ms() -> int:
    """Current UTC timestamp in milliseconds."""
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)
