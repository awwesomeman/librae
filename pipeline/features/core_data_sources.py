#!/usr/bin/env python3
"""Data source adapters for reusable ETL pipelines."""
from __future__ import annotations

import os
import random
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

from pipeline.features.cache_store import build_cache_key, load_cache, save_cache

_DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "cache", "binance"
)
_DEFAULT_CACHE_TTL = 300  # seconds

# ---------------------------------------------------------------------------
# Configurable pacing / retry defaults
# ---------------------------------------------------------------------------
DEFAULT_PACE_SECONDS: float = 0.15
"""Minimum delay between consecutive successful Binance requests."""

_MAX_RETRIES: int = 6
_MAX_SLEEP: float = 30.0
_BASE_SLEEP: float = 0.5


# ---------------------------------------------------------------------------
# Shared request helper
# ---------------------------------------------------------------------------

def _binance_request(
    url: str,
    params: dict,
    *,
    max_retries: int = _MAX_RETRIES,
    timeout: int = 20,
) -> list:
    """GET *url* with exponential-backoff retry on 429 / 5xx.

    Respects the ``Retry-After`` header when present.
    Raises ``RuntimeError`` with context after *max_retries* failures.
    """
    last_status = None
    for attempt in range(1, max_retries + 1):
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()

        last_status = r.status_code
        if r.status_code in (429, 418):
            retry_after = r.headers.get("Retry-After")
            if retry_after is not None:
                sleep_secs = min(float(retry_after), _MAX_SLEEP)
            else:
                sleep_secs = min(
                    _BASE_SLEEP * (2 ** (attempt - 1)) + random.uniform(0, 0.3),
                    _MAX_SLEEP,
                )
            time.sleep(sleep_secs)
            continue

        if 500 <= r.status_code < 600:
            time.sleep(min(_BASE_SLEEP * (2 ** (attempt - 1)), _MAX_SLEEP))
            continue

        r.raise_for_status()

    raise RuntimeError(
        f"Binance rate-limit retries exceeded: endpoint={url} "
        f"last_status={last_status} retries={max_retries}"
    )


# ---------------------------------------------------------------------------
# Pacing helper
# ---------------------------------------------------------------------------

_last_request_ts: float = 0.0


def _pace(seconds: float = DEFAULT_PACE_SECONDS) -> None:
    """Sleep just enough to maintain *seconds* gap between calls."""
    global _last_request_ts
    now = time.monotonic()
    elapsed = now - _last_request_ts
    if elapsed < seconds:
        time.sleep(seconds - elapsed)
    _last_request_ts = time.monotonic()


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Binance spot
# ---------------------------------------------------------------------------

_SPOT_KLINES_LIMIT = 1000

# Map interval strings to approximate milliseconds for chunking.
_INTERVAL_MS: dict[str, int] = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
    "1w": 604_800_000,
}


def _chunk_ranges(start_ms: int, end_ms: int, interval: str, per_request_limit: int) -> list[tuple[int, int]]:
    """Split [start_ms, end_ms) into time-window chunks.

    Each chunk covers at most *per_request_limit* candles so that the inner
    pagination loop stays short and reduces burst pressure.
    """
    bar_ms = _INTERVAL_MS.get(interval)
    if bar_ms is None:
        return [(start_ms, end_ms)]
    chunk_ms = bar_ms * per_request_limit * 3  # ~3 pages per chunk
    if chunk_ms <= 0 or (end_ms - start_ms) <= chunk_ms:
        return [(start_ms, end_ms)]
    chunks = []
    cur = start_ms
    while cur < end_ms:
        chunks.append((cur, min(cur + chunk_ms, end_ms)))
        cur += chunk_ms
    return chunks


def fetch_binance_spot_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    base_url: str = "https://api.binance.com",
    *,
    pace: float = DEFAULT_PACE_SECONDS,
    cache_dir: Optional[str] = None,
    cache_ttl_seconds: int = _DEFAULT_CACHE_TTL,
) -> pd.DataFrame:
    _cache_dir = cache_dir if cache_dir is not None else _DEFAULT_CACHE_DIR
    cache_key = build_cache_key(
        "binance_spot",
        {"symbol": symbol, "interval": interval, "start_ms": start_ms, "end_ms": end_ms},
    )
    cached = load_cache(_cache_dir, cache_key, cache_ttl_seconds)
    if cached is not None:
        raw = pd.DataFrame(cached, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbv", "tqv", "ignore",
        ])
        raw["ts"] = pd.to_datetime(raw["open_time"], unit="ms", utc=True)
        return normalize_ohlcv(raw, ts_col="ts")

    url = f"{base_url}/api/v3/klines"
    limit = _SPOT_KLINES_LIMIT
    rows: list = []

    for chunk_start, chunk_end in _chunk_ranges(start_ms, end_ms, interval, limit):
        cur = chunk_start
        while cur < chunk_end:
            _pace(pace)
            params = {"symbol": symbol, "interval": interval,
                      "startTime": cur, "endTime": chunk_end, "limit": limit}
            payload = _binance_request(url, params)
            if not payload:
                break
            rows.extend(payload)
            cur = int(payload[-1][0]) + 1
            if len(payload) < limit:
                break

    save_cache(_cache_dir, cache_key, rows)
    raw = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbv", "tqv", "ignore",
    ])
    raw["ts"] = pd.to_datetime(raw["open_time"], unit="ms", utc=True)
    return normalize_ohlcv(raw, ts_col="ts")


# ---------------------------------------------------------------------------
# Binance futures
# ---------------------------------------------------------------------------

_FUTURES_KLINES_LIMIT = 1000


def fetch_binance_futures_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    base_url: str = "https://fapi.binance.com",
    *,
    pace: float = DEFAULT_PACE_SECONDS,
    cache_dir: Optional[str] = None,
    cache_ttl_seconds: int = _DEFAULT_CACHE_TTL,
) -> pd.DataFrame:
    _cache_dir = cache_dir if cache_dir is not None else _DEFAULT_CACHE_DIR
    cache_key = build_cache_key(
        "binance_futures",
        {"symbol": symbol, "interval": interval, "start_ms": start_ms, "end_ms": end_ms},
    )
    cached = load_cache(_cache_dir, cache_key, cache_ttl_seconds)
    if cached is not None:
        raw = pd.DataFrame(cached, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbv", "tqv", "ignore",
        ])
        raw["ts"] = pd.to_datetime(raw["open_time"], unit="ms", utc=True)
        return normalize_ohlcv(raw, ts_col="ts")

    url = f"{base_url}/fapi/v1/klines"
    limit = _FUTURES_KLINES_LIMIT
    rows: list = []

    for chunk_start, chunk_end in _chunk_ranges(start_ms, end_ms, interval, limit):
        cur = chunk_start
        while cur < chunk_end:
            _pace(pace)
            params = {"symbol": symbol, "interval": interval,
                      "startTime": cur, "endTime": chunk_end, "limit": limit}
            payload = _binance_request(url, params)
            if not payload:
                break
            rows.extend(payload)
            cur = int(payload[-1][0]) + 1
            if len(payload) < limit:
                break

    save_cache(_cache_dir, cache_key, rows)
    raw = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbv", "tqv", "ignore",
    ])
    raw["ts"] = pd.to_datetime(raw["open_time"], unit="ms", utc=True)
    return normalize_ohlcv(raw, ts_col="ts")


# ---------------------------------------------------------------------------
# Shioaji
# ---------------------------------------------------------------------------

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
