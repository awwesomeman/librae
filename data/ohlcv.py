"""Unified OHLCV data access — DB-first with API fallback.

Single entry point for all OHLCV data needs (backtest, sim, pipeline).
Checks DB for existing data, fetches gaps from exchange API, and upserts
results back to DB.

    df = get_ohlcv("BTCUSDT", "1h", data_source="binance_spot", start="2025-10-01", end="2026-04-01")
    df = get_ohlcv("TXFR1", "5m", data_source="shioaji", start="2025-01-01", warmup_periods=200)

Adding a new data source
------------------------
Register a fetcher function with ``register_ohlcv_fetcher`` before calling
``get_ohlcv``.  The fetcher signature is::

    def my_fetcher(
        symbol: str,
        interval: str,      # ccxt format, e.g. "1h", "5m"
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:      # columns: timestamp, open, high, low, close, volume
        ...

    register_ohlcv_fetcher("custom_exchange", my_fetcher)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from data.binance import fetch_ohlcv as _binance_fetch_ohlcv
from data.utils import parse_dt
from librae.core.utils import interval_to_timedelta, to_canonical, to_ccxt

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# ---------------------------------------------------------------------------
# Fetcher registry
# ---------------------------------------------------------------------------

# data_source → callable(symbol, interval, start, end) → DataFrame
_OHLCV_FETCHERS: dict[str, Callable] = {}


def register_ohlcv_fetcher(data_source: str, fn: Callable) -> None:
    """Register a data-source fetcher under ``data_source`` name.

    The fetcher will be called by ``get_ohlcv`` when ``data_source=data_source``
    is requested.  Registering a name that already exists overwrites it.

    Args:
        data_source: Identifier string (e.g. ``'binance_spot'``, ``'shioaji'``).
        fn: ``fn(symbol, interval, start, end) -> DataFrame`` where
            ``interval`` is in ccxt format (``'1h'``, ``'5m'`` …).
    """
    _OHLCV_FETCHERS[data_source] = fn


def _binance_fetcher(
    symbol: str, interval: str, start: datetime, end: datetime,
) -> pd.DataFrame:
    return _binance_fetch_ohlcv(symbol=symbol, interval=interval, start=start, end=end, use_cache=False)


register_ohlcv_fetcher("binance_spot", _binance_fetcher)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ohlcv(
    symbol: str,
    timeframe: str,
    *,
    data_source: str,
    start: str | datetime,
    end: str | datetime | None = None,
    warmup_periods: int = 0,
) -> pd.DataFrame:
    """Unified OHLCV fetch: DB → API gap-fill → DB.

    Args:
        symbol:          Trading symbol (e.g. ``'BTCUSDT'``, ``'TXFR1'``).
        timeframe:       Candle interval in any supported format
                         (ccxt: ``'1h'``, ``'5m'``; canonical: ``'H1'``, ``'M5'``).
        data_source:     Data source key registered via ``register_ohlcv_fetcher``.
                         Built-in: ``'binance_spot'``.
        start:           Start of the requested time range.
        end:             End of the requested time range. Defaults to now.
        warmup_periods:  Extra bars to fetch before ``start`` for indicator warm-up.
                         Default 0 (no warm-up).

    Returns:
        DataFrame with columns [timestamp, open, high, low, close, volume],
        timestamp is tz-aware UTC, sorted ascending.

    Raises:
        ValueError: If ``data_source`` has no registered fetcher.
    """
    if data_source not in _OHLCV_FETCHERS:
        raise ValueError(
            f"No OHLCV fetcher registered for data_source='{data_source}'. "
            f"Available: {sorted(_OHLCV_FETCHERS)}. "
            "Register one with register_ohlcv_fetcher()."
        )

    end_dt = parse_dt(end) if end else datetime.now(timezone.utc)
    tf_ccxt = to_ccxt(timeframe)
    start_dt = parse_dt(start)

    # Extend start backward for warm-up bars
    if warmup_periods > 0:
        start_dt = start_dt - interval_to_timedelta(tf_ccxt) * warmup_periods

    # 1. Try DB first
    db_df = _query_db(symbol, tf_ccxt, start_dt, end_dt, data_source)

    if db_df is not None and not db_df.empty:
        gaps = _find_gaps(db_df, start_dt, end_dt, tf_ccxt)
        if not gaps:
            logger.debug("DB hit: %s %s (%d bars)", symbol, tf_ccxt, len(db_df))
            return db_df
        logger.info("DB partial: %s %s, %d gaps to fill", symbol, tf_ccxt, len(gaps))
    else:
        gaps = [(start_dt, end_dt)]
        logger.info("DB miss: %s %s, fetching full range from API", symbol, tf_ccxt)

    # 2. Fill gaps from API → upsert to DB
    fetched_parts: list[pd.DataFrame] = []
    for gap_start, gap_end in gaps:
        api_df = _fetch_from_api(symbol, tf_ccxt, gap_start, gap_end, data_source)
        if not api_df.empty:
            fetched_parts.append(api_df)
            _upsert_db(api_df, symbol, tf_ccxt, data_source)

    # 3. Re-read from DB (merges existing + newly upserted data)
    db_df = _query_db(symbol, tf_ccxt, start_dt, end_dt, data_source)
    if db_df is not None and not db_df.empty:
        return db_df

    # 4. Fallback: DB unavailable, return already-fetched API data
    if fetched_parts:
        return pd.concat(fetched_parts, ignore_index=True)
    return _fetch_from_api(symbol, tf_ccxt, start_dt, end_dt, data_source)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _query_db(
    symbol: str, tf_ccxt: str, start_dt: datetime, end_dt: datetime,
    data_source: str,
) -> pd.DataFrame | None:
    """Query ohlcv table. Returns None if DB is unavailable."""
    try:
        from db.timescale_reader import load_ohlcv

        df = load_ohlcv(
            symbol=symbol, timeframe=to_canonical(tf_ccxt),
            data_source=data_source,
            start_ts=start_dt.isoformat(), end_ts=end_dt.isoformat(),
        )
        if df.empty:
            return df
        df = df.rename(columns={"_time": "timestamp"})
        return df[OHLCV_COLUMNS].reset_index(drop=True)
    except Exception as e:
        logger.warning("DB query failed: %s", e)
        return None


def _fetch_from_api(
    symbol: str,
    tf_ccxt: str,
    start_dt: datetime,
    end_dt: datetime,
    data_source: str,
) -> pd.DataFrame:
    """Dispatch fetch to the registered fetcher for ``data_source``."""
    fetcher = _OHLCV_FETCHERS[data_source]
    return fetcher(symbol, tf_ccxt, start_dt, end_dt)


def _upsert_db(
    df: pd.DataFrame, symbol: str, tf_ccxt: str, data_source: str,
) -> None:
    """Write OHLCV to DB via existing writer."""
    try:
        from db.timescale_writer import write_ohlcv

        work = df
        if "timestamp" in work.columns:
            work = work.set_index("timestamp")
            work.index.name = "ts"
        write_ohlcv(work, symbol, tf_ccxt, data_source=data_source)
    except Exception as e:
        logger.warning("DB upsert failed: %s", e)


def _find_gaps(
    db_df: pd.DataFrame,
    start_dt: datetime,
    end_dt: datetime,
    tf_ccxt: str,
) -> list[tuple[datetime, datetime]]:
    """Find missing time ranges in DB data.

    Returns list of (gap_start, gap_end) tuples. Empty list = no gaps.
    """
    if db_df.empty:
        return [(start_dt, end_dt)]

    ts_col = "timestamp" if "timestamp" in db_df.columns else "_time"
    db_min = pd.Timestamp(db_df[ts_col].min()).to_pydatetime()
    db_max = pd.Timestamp(db_df[ts_col].max()).to_pydatetime()

    if db_min.tzinfo is None:
        db_min = db_min.replace(tzinfo=timezone.utc)
    if db_max.tzinfo is None:
        db_max = db_max.replace(tzinfo=timezone.utc)

    delta = interval_to_timedelta(tf_ccxt)

    gaps: list[tuple[datetime, datetime]] = []
    if db_min > start_dt + delta:
        gaps.append((start_dt, db_min))
    if db_max < end_dt - delta:
        gaps.append((db_max, end_dt))

    return gaps
