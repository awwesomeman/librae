#!/usr/bin/env python3
"""Data source adapters for reusable ETL pipelines."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests


def normalize_ohlcv(df: pd.DataFrame, ts_col: str = "ts") -> pd.DataFrame:
    out = df.copy()
    if ts_col != "ts":
        out = out.rename(columns={ts_col: "ts"})
    keep = ["ts", "open", "high", "low", "close", "volume"]
    out = out[keep]
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna().set_index("ts").sort_index()


def fetch_binance_spot_klines(symbol: str, interval: str, start_ms: int, end_ms: int, base_url: str = "https://api.binance.com") -> pd.DataFrame:
    rows = []
    cur = start_ms
    while cur < end_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": cur, "endTime": end_ms, "limit": 1000}
        payload = None
        for _ in range(6):
            r = requests.get(f"{base_url}/api/v3/klines", params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.2)
                continue
            r.raise_for_status()
            payload = r.json()
            break
        if payload is None:
            raise RuntimeError("binance spot rate-limit retries exceeded")
        if not payload:
            break
        rows.extend(payload)
        cur = int(payload[-1][0]) + 1
        if len(payload) < 1000:
            break
        time.sleep(0.08)

    raw = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "tbv", "tqv", "ignore"])
    raw["ts"] = pd.to_datetime(raw["open_time"], unit="ms", utc=True)
    return normalize_ohlcv(raw, ts_col="ts")


def fetch_binance_futures_klines(symbol: str, interval: str, start_ms: int, end_ms: int, base_url: str = "https://fapi.binance.com") -> pd.DataFrame:
    rows = []
    cur = start_ms
    while cur < end_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": cur, "endTime": end_ms, "limit": 1500}
        payload = None
        for _ in range(5):
            r = requests.get(f"{base_url}/fapi/v1/klines", params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.2)
                continue
            r.raise_for_status()
            payload = r.json()
            break
        if payload is None:
            raise RuntimeError("binance futures rate-limit retries exceeded")
        if not payload:
            break
        rows.extend(payload)
        cur = int(payload[-1][0]) + 1
        if len(payload) < 1500:
            break
        time.sleep(0.12)

    raw = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "tbv", "tqv", "ignore"])
    raw["ts"] = pd.to_datetime(raw["open_time"], unit="ms", utc=True)
    return normalize_ohlcv(raw, ts_col="ts")


def fetch_shioaji_mxfr1_1m(start: str, end: str, simulation: bool = True) -> pd.DataFrame:
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
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)
