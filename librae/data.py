"""Data utilities — fetch OHLCV, resample, cache.

Generic data operations used by strategy utils.py and tests.
Exchange-specific logic (Binance API) is encapsulated here so strategies
only call `fetch_ohlcv()` without knowing the data source details.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

# ---------------------------------------------------------------------------
# Binance OHLCV fetcher
# ---------------------------------------------------------------------------

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_LIMIT = 1000

_DEFAULT_CACHE_DIR = Path("data/cache")
_CACHE_MAX_AGE = timedelta(hours=6)


def _cache_path(symbol: str, interval: str, cache_dir: Path | None = None) -> Path:
    d = cache_dir or _DEFAULT_CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{symbol}_{interval}.parquet"


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
    except Exception:
        return False


def fetch_ohlcv(
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    months: int = 6,
    timeout: float = 30.0,
    use_cache: bool = True,
    cache_dir: Path | None = None,
) -> pd.DataFrame:
    """Fetch OHLCV klines from Binance REST API.

    Returns DataFrame with columns: timestamp, open, high, low, close, volume.
    timestamp is tz-aware UTC.
    """
    cpath = _cache_path(symbol, interval, cache_dir)
    if use_cache and _is_cache_fresh(cpath):
        return pd.read_parquet(cpath)

    end_dt = _parse_dt(end) if end else datetime.now(timezone.utc)
    start_dt = _parse_dt(start) if start else _subtract_months(end_dt, months)

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_rows: list[list] = []

    with httpx.Client(timeout=timeout) as client:
        cursor_ms = start_ms
        while cursor_ms < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor_ms,
                "endTime": end_ms,
                "limit": MAX_LIMIT,
            }
            resp = client.get(BINANCE_KLINES_URL, params=params)
            resp.raise_for_status()
            klines = resp.json()
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
                except Exception:
                    pass
            df.to_parquet(cpath, index=False)
        except Exception:
            pass

    return df


# ---------------------------------------------------------------------------
# OHLCV resampling
# ---------------------------------------------------------------------------


def resample_ohlcv(df: pd.DataFrame, rule: str = "1D") -> pd.DataFrame:
    """Resample OHLCV DataFrame to a different timeframe.

    Args:
        df: DataFrame with DatetimeIndex and open/high/low/close/volume columns.
        rule: Pandas resample rule (e.g. "1D", "4h", "1W").
    """
    x = pd.DataFrame()
    x["open"] = df["open"].resample(rule).first()
    x["high"] = df["high"].resample(rule).max()
    x["low"] = df["low"].resample(rule).min()
    x["close"] = df["close"].resample(rule).last()
    x["volume"] = df["volume"].resample(rule).sum()
    return x.dropna()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _subtract_months(dt: datetime, months: int) -> datetime:
    month = dt.month - months
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(dt.day, 28)
    return dt.replace(year=year, month=month, day=day)
