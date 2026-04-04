"""Binance public API — fetch OHLCV with local cache.

Uses the unauthenticated ``/api/v3/klines`` endpoint.
Generic utilities (resample, datetime parsing) live in ``data.utils``.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

from data.utils import parse_dt, subtract_months

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Binance OHLCV fetcher
# ---------------------------------------------------------------------------

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_LIMIT = 1000
_MAX_RETRIES = 5
_BASE_SLEEP = 0.5
_MAX_SLEEP = 30.0

_DEFAULT_CACHE_DIR = Path("data/cache")
_CACHE_MAX_AGE = timedelta(hours=6)


def _cache_path(symbol: str, interval: str, source: str = "binance_spot", cache_dir: Path | None = None) -> Path:
    d = cache_dir or _DEFAULT_CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{symbol}_{interval}_{source}.parquet"


def _is_cache_fresh(path: Path, now: datetime | None = None) -> bool:
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
        if df.empty:
            return False
        latest_ts = pd.Timestamp(df["timestamp"].max())
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.tz_localize("UTC")
        ref = now or datetime.now(timezone.utc)
        return (ref - latest_ts) < _CACHE_MAX_AGE
    except Exception as exc:
        logger.warning("Cache read failed for %s: %s", path, exc)
        return False


def _trim_range(df: pd.DataFrame, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """Trim DataFrame to [start_dt, end_dt] range. Prevents look-ahead bias from cache."""
    if df.empty:
        return df
    ts = pd.to_datetime(df["timestamp"], utc=True)
    return df[(ts >= start_dt) & (ts <= end_dt)].reset_index(drop=True)


def _request_with_retry(
    client: httpx.Client,
    url: str,
    params: dict,
    max_retries: int = _MAX_RETRIES,
) -> list:
    """GET with exponential backoff on 429/5xx. Respects Retry-After header."""
    for attempt in range(1, max_retries + 1):
        resp = client.get(url, params=params)
        if resp.status_code == 200:
            return resp.json()

        if resp.status_code in (429, 418):
            retry_after = resp.headers.get("Retry-After")
            sleep_secs = (
                min(float(retry_after), _MAX_SLEEP) if retry_after
                else min(_BASE_SLEEP * (2 ** (attempt - 1)) + random.uniform(0, 0.3), _MAX_SLEEP)
            )
            logger.warning("Rate limited (attempt %d/%d), sleeping %.1fs", attempt, max_retries, sleep_secs)
            time.sleep(sleep_secs)
            continue

        if 500 <= resp.status_code < 600:
            sleep_secs = min(_BASE_SLEEP * (2 ** (attempt - 1)), _MAX_SLEEP)
            logger.warning("Server error %d (attempt %d/%d), retrying in %.1fs", resp.status_code, attempt, max_retries, sleep_secs)
            time.sleep(sleep_secs)
            continue

        resp.raise_for_status()

    raise RuntimeError(f"Binance API retries exhausted: {url} after {max_retries} attempts")


def fetch_ohlcv(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    months: int = 6,
    timeout: float = 30.0,
    use_cache: bool = True,
    cache_dir: Path | None = None,
    source: str = "binance_spot",
) -> pd.DataFrame:
    """Fetch OHLCV klines from Binance REST API.

    Returns DataFrame with columns: timestamp, open, high, low, close, volume.
    timestamp is tz-aware UTC.
    """
    end_dt = parse_dt(end) if end else datetime.now(timezone.utc)
    start_dt = parse_dt(start) if start else subtract_months(end_dt, months)

    cpath = _cache_path(symbol, interval, source, cache_dir)
    if use_cache and _is_cache_fresh(cpath):
        return _trim_range(pd.read_parquet(cpath), start_dt, end_dt)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_rows: list[list] = []

    with httpx.Client(timeout=timeout) as client:
        cursor_ms = start_ms
        while cursor_ms < end_ms:
            req_params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor_ms,
                "endTime": end_ms,
                "limit": MAX_LIMIT,
            }
            klines = _request_with_retry(client, BINANCE_KLINES_URL, req_params)
            if not klines:
                break
            all_rows.extend(klines)
            last_open_time = klines[-1][0]
            cursor_ms = last_open_time + 1
            if len(klines) < MAX_LIMIT:
                break
            time.sleep(0.1)

    if not all_rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    if use_cache and not df.empty:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            if cpath.exists():
                try:
                    existing = pd.read_parquet(cpath)
                    merged = pd.concat([existing, df], ignore_index=True)
                    merged = merged.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                    df = merged
                except Exception as exc:
                    logger.warning("Cache merge failed, overwriting: %s", exc)
            df.to_parquet(cpath, index=False)
        except Exception as exc:
            logger.warning("Cache write failed: %s", exc)

    return _trim_range(df, start_dt, end_dt)
