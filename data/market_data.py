"""Unified market data access — DB-first with API fallback.

Single entry point for all OHLCV data needs (backtest, sim, pipeline).
Checks DB for existing data, fetches gaps from exchange API, and upserts
results back to DB.

    df = get_ohlcv("BTCUSDT", "1h", months=6)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


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
        symbol: Trading pair (e.g. "BTCUSDT").
        interval: Candle interval (e.g. "1h", "5m", "1d").
        start/end: Time range. If omitted, uses ``months`` before now.
        months: Lookback period when start is not specified.
        source: Data source identifier for multi-exchange support.

    Returns:
        DataFrame with columns [timestamp, open, high, low, close, volume],
        timestamp is tz-aware UTC, sorted ascending.
    """
    from data.binance import _parse_dt, _subtract_months

    end_dt = _parse_dt(end) if end else datetime.now(timezone.utc)
    start_dt = _parse_dt(start) if start else _subtract_months(end_dt, months)

    # 1. Try DB first
    db_df = _query_db(symbol, interval, start_dt, end_dt)

    if db_df is not None and not db_df.empty:
        gaps = _find_gaps(db_df, start_dt, end_dt, interval)
        if not gaps:
            logger.debug("DB hit: %s %s (%d bars)", symbol, interval, len(db_df))
            return db_df
        logger.info("DB partial: %s %s, %d gaps to fill", symbol, interval, len(gaps))
    else:
        gaps = [(start_dt, end_dt)]
        logger.info("DB miss: %s %s, fetching full range from API", symbol, interval)

    # 2. Fill gaps from API → upsert to DB
    fetched_parts: list[pd.DataFrame] = []
    for gap_start, gap_end in gaps:
        api_df = _fetch_from_api(symbol, interval, gap_start, gap_end)
        if not api_df.empty:
            fetched_parts.append(api_df)
            _upsert_db(api_df, symbol, interval, source)

    # 3. Re-read from DB (merges existing + newly upserted data)
    db_df = _query_db(symbol, interval, start_dt, end_dt)
    if db_df is not None and not db_df.empty:
        return db_df

    # 4. Fallback: DB unavailable, return already-fetched API data
    if fetched_parts:
        return pd.concat(fetched_parts, ignore_index=True)
    return _fetch_from_api(symbol, interval, start_dt, end_dt)


def _query_db(
    symbol: str, interval: str, start_dt: datetime, end_dt: datetime,
) -> pd.DataFrame | None:
    """Query ohlcv table. Returns None if DB is unavailable."""
    try:
        from db.timescale_reader import load_ohlcv

        df = load_ohlcv(
            symbol=symbol, timeframe=interval,
            start_ts=start_dt.isoformat(), end_ts=end_dt.isoformat(),
        )
        if df.empty:
            return df
        # Normalise column name: _time → timestamp
        df = df.rename(columns={"_time": "timestamp"})
        return df[OHLCV_COLUMNS].reset_index(drop=True)
    except Exception as e:
        logger.warning("DB query failed: %s", e)
        return None


def _fetch_from_api(
    symbol: str, interval: str, start_dt: datetime, end_dt: datetime,
) -> pd.DataFrame:
    """Fetch from Binance API with pagination."""
    from data.binance import fetch_ohlcv

    # WHY: use_cache=False because we manage caching via DB now
    return fetch_ohlcv(
        symbol=symbol, interval=interval,
        start=start_dt, end=end_dt,
        use_cache=False,
    )


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

    # Ensure timezone-aware
    if db_min.tzinfo is None:
        db_min = db_min.replace(tzinfo=timezone.utc)
    if db_max.tzinfo is None:
        db_max = db_max.replace(tzinfo=timezone.utc)

    gaps: list[tuple[datetime, datetime]] = []
    delta = _interval_to_timedelta(interval)

    # Gap at the start
    if db_min > start_dt + delta:
        gaps.append((start_dt, db_min))

    # Gap at the end
    if db_max < end_dt - delta:
        gaps.append((db_max, end_dt))

    return gaps


def _interval_to_timedelta(interval: str) -> pd.Timedelta:
    """Convert interval string to timedelta. Delegates to shared utility."""
    from librae.core.utils import interval_to_timedelta
    return interval_to_timedelta(interval)
