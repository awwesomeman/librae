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
                             frequency="D1", instrument_type="contract_perpetual")

Call ``sync_factor_registry()`` once after importing whichever factor
modules you need (e.g. at the top of a collection script) to push the
registered (factor_name, source, frequency) rows into the DB's
``factor_registry`` table — that's what ``data_inventory`` reads frequency
from.

Path B — snapshot-only sources (collect_snapshot_factor / load_snapshot_factor)
-------------------------------------------------------------------------
get_factor()'s coverage-tracked gap-fill assumes a fetcher can answer an
arbitrary historical range — call it once with a wide [start, end], the
whole range gets marked "covered" and is never re-fetched. That assumption
breaks for APIs that only ever answer "what's true right now" or a fixed
trailing window (ApeWisdom's live mention ranking, Finnhub's free-tier
recommendation/earnings — verified live: `from`/`to` params are silently
ignored, always the same trailing N periods regardless of range asked).
Wiring one of those through register_factor_fetcher() would mark the
entire requested range covered after the first call and then silently
never notice any new period that appears later — same failure mode that
originally forced ``us_chip.py``'s short interest off yfinance.

For these, call collect_snapshot_factor() periodically (cron/manual) — no
coverage bookkeeping, just append whatever's new — and read back with
load_snapshot_factor(), not get_factor() (which would reject an
unregistered factor_name outright).

    collect_snapshot_factor("MU", "us_social_mentions", "apewisdom", 473, frequency="H1")
    df = load_snapshot_factor("MU", "us_social_mentions", "apewisdom", start="2026-01-01")
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from strategies.module.data.utils import compute_coverage_gaps, parse_dt

logger = logging.getLogger(__name__)

FACTOR_COLUMNS = ["timestamp", "value"]

# Sentinel `frequency` for factors with no fixed grid (real-world event
# dates like dividends/splits) — deliberately outside librae.core.utils'
# canonical M/H/D/W/MN vocabulary, which only covers actual regular
# intervals; giving it a name here (not a bare string) avoids typos.
FREQUENCY_IRREGULAR = "IRREGULAR"

# ---------------------------------------------------------------------------
# Fetcher registry
# ---------------------------------------------------------------------------

# factor_name -> (fetcher, source label, instrument_type, frequency) stored alongside cached rows
_FACTOR_FETCHERS: dict[str, tuple[Callable, str, str, str]] = {}


def register_factor_fetcher(
    factor_name: str, fn: Callable, *, source: str, frequency: str, instrument_type: str = "spot",
) -> None:
    """Register a fetcher under ``factor_name``.

    Args:
        factor_name: Identifier string (e.g. ``'funding_rate'``, ``'open_interest'``).
        fn: ``fn(symbol, start, end) -> DataFrame`` with columns [timestamp, value].
        source: Label recorded on every cached row (e.g. ``'binanceusdm'``) —
            purely descriptive, not a separate selection axis; one
            factor_name maps to exactly one fetcher/source.
        frequency: How often this factor actually gets new data, stated from
            domain knowledge — hardcoded here, not inferred from timestamp
            gaps in whatever happens to be cached so far (a fresh/sparse
            factor's actual gaps are a bad estimate of its true frequency).
            Same canonical vocabulary as ohlcv timeframes (librae/core/utils
            .to_canonical: ``'M5'``, ``'H1'``, ``'D1'``, ``'W1'``, ``'MN3'``
            ...), plus ``'IRREGULAR'`` for real-world event dates with no
            fixed grid (dividends, splits). Surfaced in the
            ``factor_registry`` table via ``sync_factor_registry()``, one
            row per factor_name — not repeated on every cached row.
        instrument_type: Contract expiry structure this factor is inherently
            about (see librae/config/symbols.py's ALLOWED_INSTRUMENT_TYPES).
            Fixed per factor_name, not resolved per-symbol — funding_rate and
            open_interest are perpetual-futures concepts regardless of which
            symbol string is passed (e.g. Binance's spot and USDM-perpetual
            APIs both use the ticker "BTCUSDT", so this can't be inferred
            from the symbol alone).
    """
    _FACTOR_FETCHERS[factor_name] = (fn, source, instrument_type, frequency)


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
    fetcher, source, instrument_type, _frequency = _FACTOR_FETCHERS[factor_name]

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


def sync_factor_registry() -> None:
    """Upsert every currently-registered (factor_name, source, frequency)
    into the DB's ``factor_registry`` table. Call once after importing the
    factor modules you need — not at import time inside
    register_factor_fetcher() itself, which must stay DB-free so importing
    a factor module never requires a DB connection to be configured."""
    try:
        from db.timescale_writer import write_factor_registry
        write_factor_registry([
            {"factor_name": name, "source": source, "frequency": frequency}
            for name, (_fn, source, _instrument_type, frequency) in _FACTOR_FETCHERS.items()
        ])
    except Exception as e:
        logger.warning("factor_registry sync failed: %s", e)


# ---------------------------------------------------------------------------
# Path B — snapshot-only sources (see module docstring for when to use this
# instead of register_factor_fetcher()/get_factor())
# ---------------------------------------------------------------------------

def collect_snapshot_factor(
    symbol: str, factor_name: str, source: str, value: float, *,
    frequency: str, ts: datetime | None = None, instrument_type: str = "spot",
) -> int:
    """Append one snapshot reading to external_factors — no coverage-range
    bookkeeping, so get_factor() will reject `factor_name` outright (it
    checks the fetcher registry first). Read back with load_snapshot_factor().

    `ts` defaults to now — most snapshot APIs give no per-entry timestamp of
    their own (see e.g. ApeWisdom), so collection time is the only honest
    "as of" for the row. Pass one explicitly when the source does have a
    real as-of date (e.g. an earnings period end).
    """
    from db.timescale_writer import write_external_factor, write_factor_registry

    # Piggybacked on this already-DB-touching call rather than at import
    # time — same reasoning as sync_factor_registry(): a factor module must
    # stay importable without a DB connection configured.
    write_factor_registry([{"factor_name": factor_name, "source": source, "frequency": frequency}])

    row_ts = pd.Timestamp(ts) if ts is not None else pd.Timestamp(datetime.now(timezone.utc))
    df = pd.DataFrame([{"timestamp": row_ts, "value": float(value)}])
    return write_external_factor(df, symbol, factor_name, source, instrument_type=instrument_type)


def load_snapshot_factor(
    symbol: str, factor_name: str, source: str, *,
    start: str | None = None, end: str | None = None, instrument_type: str = "spot",
) -> pd.DataFrame:
    """Read back what collect_snapshot_factor() has written so far — direct DB
    read, bypasses get_factor()'s fetcher-registry requirement."""
    from db.timescale_reader import load_external_factor

    return load_external_factor(symbol, factor_name, source, instrument_type=instrument_type, started_at=start, ended_at=end)


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
