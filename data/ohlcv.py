"""Unified OHLCV data access — DB-first with API fallback.

Single entry point for all OHLCV data needs (backtest, sim, pipeline).
Checks DB for existing data, fetches gaps from exchange API, and upserts
results back to DB.

    df = get_ohlcv("BTCUSDT", "1h", months=6)
    df = get_ohlcv("TXFR1", "5m", months=3, source="shioaji")

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

    register_ohlcv_fetcher("my_source", my_fetcher)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from data.binance import fetch_ohlcv as _binance_fetch_ohlcv
from data.binance import _parse_dt, _subtract_months
from librae.core.utils import interval_to_timedelta, to_canonical, to_ccxt

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# ---------------------------------------------------------------------------
# Fetcher registry
# ---------------------------------------------------------------------------

# source_name → callable(symbol, interval, start, end) → DataFrame
_OHLCV_FETCHERS: dict[str, Callable] = {}


def register_ohlcv_fetcher(source: str, fn: Callable) -> None:
    """Register a data-source fetcher under ``source`` name.

    The fetcher will be called by ``get_ohlcv`` when ``source=source`` is
    requested.  Registering a name that already exists overwrites it.

    Args:
        source: Identifier string (e.g. ``'binance_spot'``, ``'shioaji'``).
        fn: ``fn(symbol, interval, start, end) -> DataFrame`` where
            ``interval`` is in ccxt format (``'1h'``, ``'5m'`` …).
    """
    _OHLCV_FETCHERS[source] = fn


def _binance_fetcher(
    symbol: str, interval: str, start: datetime, end: datetime,
) -> pd.DataFrame:
    return _binance_fetch_ohlcv(symbol=symbol, interval=interval, start=start, end=end, use_cache=False)


# Built-in: Binance spot (registered under two names for backward compat)
register_ohlcv_fetcher("binance_spot", _binance_fetcher)
register_ohlcv_fetcher("binance", _binance_fetcher)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ohlcv(
    symbol: str,
    interval: str,
    *,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    months: int = 6,
    source: str = "binance_spot",
) -> pd.DataFrame:
    """Unified OHLCV fetch: DB → API gap-fill → DB.

    Args:
        symbol:   Trading symbol (e.g. ``'BTCUSDT'``, ``'TXFR1'``).
        interval: Candle interval in any supported format
                  (ccxt: ``'1h'``, ``'5m'``; canonical: ``'H1'``, ``'M5'``).
        start/end: Time range. If omitted, uses ``months`` before now.
        months:   Lookback period when ``start`` is not specified.
        source:   Data source key registered via ``register_ohlcv_fetcher``.
                  Built-in: ``'binance_spot'`` / ``'binance'``.

    Returns:
        DataFrame with columns [timestamp, open, high, low, close, volume],
        timestamp is tz-aware UTC, sorted ascending.

    Raises:
        ValueError: If ``source`` has no registered fetcher.
    """
    if source not in _OHLCV_FETCHERS:
        raise ValueError(
            f"No OHLCV fetcher registered for source='{source}'. "
            f"Available: {sorted(_OHLCV_FETCHERS)}. "
            "Register one with register_ohlcv_fetcher()."
        )

    end_dt = _parse_dt(end) if end else datetime.now(timezone.utc)
    start_dt = _parse_dt(start) if start else _subtract_months(end_dt, months)
    interval_ccxt = to_ccxt(interval)

    # 1. Try DB first
    db_df = _query_db(symbol, interval_ccxt, start_dt, end_dt, source)

    if db_df is not None and not db_df.empty:
        gaps = _find_gaps(db_df, start_dt, end_dt, interval_ccxt)
        if not gaps:
            logger.debug("DB hit: %s %s (%d bars)", symbol, interval_ccxt, len(db_df))
            return db_df
        logger.info("DB partial: %s %s, %d gaps to fill", symbol, interval_ccxt, len(gaps))
    else:
        gaps = [(start_dt, end_dt)]
        logger.info("DB miss: %s %s, fetching full range from API", symbol, interval_ccxt)

    # 2. Fill gaps from API → upsert to DB
    fetched_parts: list[pd.DataFrame] = []
    for gap_start, gap_end in gaps:
        api_df = _fetch_from_api(symbol, interval_ccxt, gap_start, gap_end, source=source)
        if not api_df.empty:
            fetched_parts.append(api_df)
            _upsert_db(api_df, symbol, interval_ccxt, source)

    # 3. Re-read from DB (merges existing + newly upserted data)
    db_df = _query_db(symbol, interval_ccxt, start_dt, end_dt, source)
    if db_df is not None and not db_df.empty:
        return db_df

    # 4. Fallback: DB unavailable, return already-fetched API data
    if fetched_parts:
        return pd.concat(fetched_parts, ignore_index=True)
    return _fetch_from_api(symbol, interval_ccxt, start_dt, end_dt, source=source)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _query_db(
    symbol: str, interval: str, start_dt: datetime, end_dt: datetime,
    source: str = "binance_spot",
) -> pd.DataFrame | None:
    """Query ohlcv table. Returns None if DB is unavailable."""
    try:
        from db.timescale_reader import load_ohlcv

        df = load_ohlcv(
            symbol=symbol, timeframe=to_canonical(interval),
            source=source,
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
    interval: str,
    start_dt: datetime,
    end_dt: datetime,
    source: str = "binance_spot",
) -> pd.DataFrame:
    """Dispatch fetch to the registered fetcher for ``source``."""
    fetcher = _OHLCV_FETCHERS[source]   # caller already validated source exists
    return fetcher(symbol, interval, start_dt, end_dt)


def _upsert_db(
    df: pd.DataFrame, symbol: str, interval: str, source: str,
) -> None:
    """Write OHLCV to DB via existing writer."""
    try:
        from db.timescale_writer import write_ohlcv

        work = df
        if "timestamp" in work.columns:
            work = work.set_index("timestamp")
            work.index.name = "ts"
        write_ohlcv(work, symbol, interval, source=source)
    except Exception as e:
        logger.warning("DB upsert failed: %s", e)


def _find_gaps(
    db_df: pd.DataFrame,
    start_dt: datetime,
    end_dt: datetime,
    interval: str,
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

    delta = interval_to_timedelta(interval)

    gaps: list[tuple[datetime, datetime]] = []
    if db_min > start_dt + delta:
        gaps.append((start_dt, db_min))
    if db_max < end_dt - delta:
        gaps.append((db_max, end_dt))

    return gaps
