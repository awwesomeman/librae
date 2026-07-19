"""Unified OHLCV data access — DB-first with API fallback.

Single entry point for all OHLCV data needs (backtest, sim, pipeline).
Tracks which time ranges are already cached per (symbol, timeframe,
data_source) in ohlcv_coverage_ranges — possibly several disjoint ranges — so a
request only pays for whatever slice isn't already covered, instead of
re-fetching the whole span between old and new data for a disjoint request.

    df = get_ohlcv("BTCUSDT", "1h", data_source="binance_spot", start="2025-10-01", end="2026-04-01")
    df = get_ohlcv("TXFR1", "5m", data_source="shioaji", start="2025-01-01", warmup_periods=200)

Adding a new data source
------------------------
Any ccxt exchange can be registered in one line via ``_ccxt_fetcher``::

    register_ohlcv_fetcher("okx_spot", _ccxt_fetcher("okx"))

For non-ccxt sources, register a fetcher function directly. The signature is::

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

from strategies.data.utils import compute_coverage_gaps, parse_dt
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


def _ccxt_fetcher(exchange_id: str) -> Callable[[str, str, datetime, datetime], pd.DataFrame]:
    """Build a get_ohlcv-compatible fetcher backed by CryptoAdapter (ccxt).

    Paginates via ``since`` until [start, end] is covered — a single ccxt
    call is capped at ~1000 bars, so large windows need multiple pages.
    Works read-only (no API key needed) for any ccxt exchange id.
    """
    def _fetch(symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        from brokers.crypto_adapter import CryptoAdapter

        adapter = CryptoAdapter(exchange_id=exchange_id)
        limit = 1000
        since_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        pages: list[pd.DataFrame] = []
        while since_ms <= end_ms:
            page = adapter.fetch_ohlcv(symbol, interval, limit, since=since_ms)
            if page.empty:
                break
            pages.append(page)
            last_ts_ms = int(page["ts"].iloc[-1].timestamp() * 1000)
            next_since_ms = last_ts_ms + 1
            if next_since_ms <= since_ms:
                break  # no progress — avoid looping forever
            since_ms = next_since_ms
            if len(page) < limit:
                break  # short page — caught up to the exchange's latest data

        if not pages:
            return pd.DataFrame(columns=OHLCV_COLUMNS)

        df = pd.concat(pages, ignore_index=True).rename(columns={"ts": "timestamp"})
        df = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)]
        return df.reset_index(drop=True)

    return _fetch


register_ohlcv_fetcher("binance_spot", _ccxt_fetcher("binance"))


def _shioaji_fetcher() -> Callable[[str, str, datetime, datetime], pd.DataFrame]:
    """Build a get_ohlcv-compatible fetcher backed by ShioajiAdapter.

    Unlike ccxt (stateless REST, no login needed for market data), Shioaji
    requires an authenticated session for every call — a read-only
    (no-CA) SHIOAJI_* key is enough (see brokers/shioaji_adapter.py).
    Login is expensive relative to a REST call, so one adapter instance is
    lazily created and reused across every gap-fill call in a process
    rather than rebuilt per call. shioaji itself isn't imported until the
    first actual fetch, so importing this module doesn't require it to be
    installed or configured.

    Shioaji's kbars API takes a date range directly (server-side, no
    client-side pagination needed) and already returns the target
    timeframe resampled on TAIFEX session boundaries — see
    ShioajiAdapter.fetch_ohlcv for the resample/timezone handling.
    """
    _adapter_holder: dict = {}

    def _get_adapter():
        if "adapter" not in _adapter_holder:
            from brokers.shioaji_adapter import ShioajiAdapter
            # Historical OHLCV is research/backtest, never order placement —
            # always simulation mode here, regardless of what the caller's
            # own live/sim mode is. Some SHIOAJI_API_KEY tokens are only
            # provisioned for simulation login (production login then fails
            # with "Token doesn't have production permission"), so this is
            # also the more broadly-compatible default, not just the safer one.
            _adapter_holder["adapter"] = ShioajiAdapter(simulation=True)
        return _adapter_holder["adapter"]

    def _fetch(symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        adapter = _get_adapter()
        df = adapter.fetch_ohlcv(symbol, interval, start=start, end=end)
        if df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = df.rename(columns={"ts": "timestamp"})
        return df[(df["timestamp"] >= start) & (df["timestamp"] <= end)].reset_index(drop=True)

    return _fetch


register_ohlcv_fetcher("shioaji", _shioaji_fetcher())


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
                         Built-in: ``'binance_spot'``, ``'shioaji'``.
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

    instrument_type = _resolve_instrument_type(symbol)
    end_dt = parse_dt(end) if end else datetime.now(timezone.utc)
    tf_ccxt = to_ccxt(timeframe)
    start_dt = parse_dt(start)

    # Extend start backward for warm-up bars
    if warmup_periods > 0:
        start_dt = start_dt - interval_to_timedelta(tf_ccxt) * warmup_periods

    # 1. Find gaps against tracked coverage (may be several disjoint ranges)
    coverage = _query_coverage(symbol, tf_ccxt, data_source, instrument_type)
    if coverage is None:
        # DB unavailable — skip caching, fetch the full range directly.
        logger.warning("Coverage lookup failed, fetching full range from API (no cache)")
        return _fetch_from_api(symbol, tf_ccxt, start_dt, end_dt, data_source)

    gaps = _compute_gaps(coverage, start_dt, end_dt)
    if not gaps:
        logger.debug("DB hit: %s %s (fully covered)", symbol, tf_ccxt)
        db_df = _query_db(symbol, tf_ccxt, start_dt, end_dt, data_source, instrument_type)
        return db_df if db_df is not None else pd.DataFrame(columns=OHLCV_COLUMNS)
    logger.info("DB partial: %s %s, %d gaps to fill", symbol, tf_ccxt, len(gaps))

    # 2. Fill gaps from API → upsert bars + mark covered (even if a gap
    # legitimately returns no bars, e.g. an exchange holiday — otherwise
    # every future call would re-fetch that same empty window forever).
    fetched_parts: list[pd.DataFrame] = []
    for gap_start, gap_end in gaps:
        api_df = _fetch_from_api(symbol, tf_ccxt, gap_start, gap_end, data_source)
        if not api_df.empty:
            fetched_parts.append(api_df)
            _upsert_db(api_df, symbol, tf_ccxt, data_source, instrument_type)
        _merge_coverage(symbol, tf_ccxt, data_source, gap_start, gap_end, instrument_type)

    # 3. Re-read from DB (merges existing + newly upserted data)
    db_df = _query_db(symbol, tf_ccxt, start_dt, end_dt, data_source, instrument_type)
    if db_df is not None and not db_df.empty:
        return db_df

    # 4. Fallback: DB unavailable, return already-fetched API data
    if fetched_parts:
        return pd.concat(fetched_parts, ignore_index=True)
    return pd.DataFrame(columns=OHLCV_COLUMNS)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_instrument_type(symbol: str) -> str:
    """Look up this symbol's contract expiry structure from symbols.yaml.

    Falls back to 'spot' (with a warning) for one-off/unregistered symbols
    (e.g. experiment tickers) — matches the existing registry-optional
    fallback pattern in librae/cli.py's _resolve_market_and_data_source.
    """
    from librae.config.symbols import get_symbol

    try:
        return get_symbol(symbol).instrument_type
    except KeyError:
        logger.warning(
            "%s not in symbols.yaml — defaulting instrument_type='spot'. "
            "Register it if this isn't actually spot.", symbol,
        )
        return "spot"


def _query_db(
    symbol: str, tf_ccxt: str, start_dt: datetime, end_dt: datetime,
    data_source: str, instrument_type: str,
) -> pd.DataFrame | None:
    """Query ohlcv table. Returns None if DB is unavailable."""
    try:
        from db.timescale_reader import load_ohlcv

        df = load_ohlcv(
            symbol=symbol, timeframe=to_canonical(tf_ccxt),
            data_source=data_source, instrument_type=instrument_type,
            started_at=start_dt.isoformat(), ended_at=end_dt.isoformat(),
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
    df: pd.DataFrame, symbol: str, tf_ccxt: str, data_source: str, instrument_type: str,
) -> None:
    """Write OHLCV to DB via existing writer."""
    try:
        from db.timescale_writer import write_ohlcv

        work = df
        if "timestamp" in work.columns:
            work = work.set_index("timestamp")
            work.index.name = "ts"
        write_ohlcv(work, symbol, tf_ccxt, data_source=data_source, instrument_type=instrument_type)
    except Exception as e:
        logger.warning("DB upsert failed: %s", e)


def _query_coverage(
    symbol: str, tf_ccxt: str, data_source: str, instrument_type: str,
) -> list[tuple[datetime, datetime]] | None:
    """Query cached coverage ranges for this key. Returns None if DB is unavailable."""
    try:
        from db.timescale_reader import get_ohlcv_coverage_ranges
        return get_ohlcv_coverage_ranges(symbol, to_canonical(tf_ccxt), data_source, instrument_type)
    except Exception as e:
        logger.warning("Coverage query failed: %s", e)
        return None


def _merge_coverage(
    symbol: str, tf_ccxt: str, data_source: str, start_dt: datetime, end_dt: datetime,
    instrument_type: str,
) -> None:
    """Mark [start_dt, end_dt] as cached for this key."""
    try:
        from db.timescale_writer import merge_ohlcv_coverage_ranges
        merge_ohlcv_coverage_ranges(
            symbol, to_canonical(tf_ccxt), data_source, start_dt, end_dt, instrument_type,
        )
    except Exception as e:
        logger.warning("Coverage merge failed: %s", e)


# Re-exported for backwards compatibility — moved to strategies/data/utils.py
# so factors.py's get_factor() can share the same gap math.
_compute_gaps = compute_coverage_gaps
