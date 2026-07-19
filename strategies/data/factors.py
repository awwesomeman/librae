"""Unified third-party factor access — DB-first with API fallback.

Same DB-first + coverage-tracked-gap-fill design as ``ohlcv.py``'s
``get_ohlcv``, generalized to any external time series that isn't raw OHLCV
(funding rate, open interest, macro/sentiment series, ...). Caching lives in
one shared table (``external_factors`` + ``external_factor_coverage_ranges``)
keyed by (symbol, factor_name, source, instrument_type) instead of a bespoke
table per data source — a new factor is a new ``factor_name``, not a migration.

    df = get_factor("BTC/USDT:USDT", "funding_rate", start="2025-10-01")
    df = get_factor("BTCUSDT", "open_interest", start="2025-01-01", end="2026-01-01")

Only for data with real fetch cost (an API call, rate limits). Features
derived on-the-fly from already-cached OHLCV (cross_asset, regime) don't
belong here — they're cheap to recompute and have no "gap" to track.

Adding a new factor source
---------------------------
    def my_fetcher(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        ...  # columns: timestamp (tz-aware UTC), value
        return df

    register_factor_fetcher("my_factor", my_fetcher, source="my-provider",
                             instrument_type="contract_perpetual")
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from strategies.data.utils import compute_coverage_gaps, parse_dt

logger = logging.getLogger(__name__)

FACTOR_COLUMNS = ["timestamp", "value"]

# ---------------------------------------------------------------------------
# Fetcher registry
# ---------------------------------------------------------------------------

# factor_name -> (fetcher, source label, instrument_type) stored alongside cached rows
_FACTOR_FETCHERS: dict[str, tuple[Callable, str, str]] = {}


def register_factor_fetcher(
    factor_name: str, fn: Callable, *, source: str, instrument_type: str = "spot",
) -> None:
    """Register a fetcher under ``factor_name``.

    Args:
        factor_name: Identifier string (e.g. ``'funding_rate'``, ``'open_interest'``).
        fn: ``fn(symbol, start, end) -> DataFrame`` with columns [timestamp, value].
        source: Label recorded on every cached row (e.g. ``'binanceusdm'``) —
            purely descriptive, not a separate selection axis; one
            factor_name maps to exactly one fetcher/source.
        instrument_type: Contract expiry structure this factor is inherently
            about (see librae/config/symbols.py's ALLOWED_INSTRUMENT_TYPES).
            Fixed per factor_name, not resolved per-symbol — funding_rate and
            open_interest are perpetual-futures concepts regardless of which
            symbol string is passed (e.g. Binance's spot and USDM-perpetual
            APIs both use the ticker "BTCUSDT", so this can't be inferred
            from the symbol alone).
    """
    _FACTOR_FETCHERS[factor_name] = (fn, source, instrument_type)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_factor(
    symbol: str,
    factor_name: str,
    *,
    start: str | datetime,
    end: str | datetime | None = None,
) -> pd.DataFrame:
    """Unified factor fetch: DB -> API gap-fill -> DB.

    Args:
        symbol:      Symbol in whatever format the registered fetcher expects
                     (e.g. ccxt perpetual format for funding_rate).
        factor_name: Factor key registered via ``register_factor_fetcher``.
        start:       Start of the requested time range.
        end:         End of the requested time range. Defaults to now.

    Returns:
        DataFrame with columns [timestamp, value], tz-aware UTC, sorted ascending.

    Raises:
        ValueError: If ``factor_name`` has no registered fetcher.
    """
    if factor_name not in _FACTOR_FETCHERS:
        raise ValueError(
            f"No fetcher registered for factor_name='{factor_name}'. "
            f"Available: {sorted(_FACTOR_FETCHERS)}. "
            "Register one with register_factor_fetcher()."
        )
    fetcher, source, instrument_type = _FACTOR_FETCHERS[factor_name]

    end_dt = parse_dt(end) if end else datetime.now(timezone.utc)
    start_dt = parse_dt(start)

    coverage = _query_coverage(symbol, factor_name, source, instrument_type)
    if coverage is None:
        logger.warning("Coverage lookup failed, fetching full range from API (no cache)")
        return fetcher(symbol, start_dt, end_dt)

    gaps = compute_coverage_gaps(coverage, start_dt, end_dt)
    if not gaps:
        logger.debug("DB hit: %s %s (fully covered)", symbol, factor_name)
        db_df = _query_db(symbol, factor_name, source, instrument_type, start_dt, end_dt)
        return db_df if db_df is not None else pd.DataFrame(columns=FACTOR_COLUMNS)
    logger.info("DB partial: %s %s, %d gaps to fill", symbol, factor_name, len(gaps))

    fetched_parts: list[pd.DataFrame] = []
    for gap_start, gap_end in gaps:
        api_df = fetcher(symbol, gap_start, gap_end)
        if not api_df.empty:
            fetched_parts.append(api_df)
            _upsert_db(api_df, symbol, factor_name, source, instrument_type)
        _merge_coverage(symbol, factor_name, source, instrument_type, gap_start, gap_end)

    db_df = _query_db(symbol, factor_name, source, instrument_type, start_dt, end_dt)
    if db_df is not None and not db_df.empty:
        return db_df

    if fetched_parts:
        return pd.concat(fetched_parts, ignore_index=True)
    return pd.DataFrame(columns=FACTOR_COLUMNS)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _query_db(
    symbol: str, factor_name: str, source: str, instrument_type: str,
    start_dt: datetime, end_dt: datetime,
) -> pd.DataFrame | None:
    try:
        from db.timescale_reader import load_external_factor

        return load_external_factor(
            symbol, factor_name, source, instrument_type=instrument_type,
            started_at=start_dt.isoformat(), ended_at=end_dt.isoformat(),
        )
    except Exception as e:
        logger.warning("DB query failed: %s", e)
        return None


def _upsert_db(
    df: pd.DataFrame, symbol: str, factor_name: str, source: str, instrument_type: str,
) -> None:
    try:
        from db.timescale_writer import write_external_factor
        write_external_factor(df, symbol, factor_name, source, instrument_type)
    except Exception as e:
        logger.warning("DB upsert failed: %s", e)


def _query_coverage(
    symbol: str, factor_name: str, source: str, instrument_type: str,
) -> list[tuple[datetime, datetime]] | None:
    try:
        from db.timescale_reader import get_external_factor_coverage_ranges
        return get_external_factor_coverage_ranges(symbol, factor_name, source, instrument_type)
    except Exception as e:
        logger.warning("Coverage query failed: %s", e)
        return None


def _merge_coverage(
    symbol: str, factor_name: str, source: str, instrument_type: str,
    start_dt: datetime, end_dt: datetime,
) -> None:
    try:
        from db.timescale_writer import merge_external_factor_coverage_ranges
        merge_external_factor_coverage_ranges(
            symbol, factor_name, source, start_dt, end_dt, instrument_type,
        )
    except Exception as e:
        logger.warning("Coverage merge failed: %s", e)
