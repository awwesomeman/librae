"""Historical open-interest and positioning data for crypto perpetuals —
all sourced from the same data.binance.vision daily archive file.

    open_interest                 — sum_open_interest (contracts), M5
    open_interest_value           — sum_open_interest_value (USD notional), M5
    top_trader_ls_account_ratio   — count_toptrader_long_short_ratio: large
                                     accounts' long/short *count* ratio, M5
    top_trader_ls_position_ratio  — sum_toptrader_long_short_ratio: large
                                     accounts' long/short *position size*
                                     ratio (a few whales dominating position
                                     size look different from account count
                                     — both worth having), M5
    global_ls_account_ratio       — count_long_short_ratio: all-accounts
                                     long/short ratio (not just large ones), M5
    taker_buy_sell_ratio          — sum_taker_long_short_vol_ratio: aggressive
                                     taker buy/sell volume ratio, M5

Binance's live REST endpoints for these (``/futures/data/topLongShortAccountRatio``
etc.) only retain ~30 days, same limitation as the live OI endpoint (see
below) — ``data.binance.vision``'s daily archive has no such retention limit
and, as it turns out, bundles all six of these metrics in one file per day
(``.../metrics/{symbol}/{symbol}-metrics-{date}.zip``) — Binance's live
OI-history REST endpoint (``fetch_open_interest_history`` via ccxt, or
``/futures/data/openInterestHist`` directly) only retains ~30 days; a `since`
outside that window is a hard 400 from the exchange, too short for
multi-month backtest windows.

Each factor still triggers its own day-by-day download when its own gap is
uncached (get_factor() has no cross-factor-name response cache — same
accepted tradeoff as us_fundamentals.py's 9 factors each independently
re-fetching one shared SEC companyfacts payload) — not efficient if you
backfill all six at once for a fresh symbol, but the files are small and
this is a one-time cost per range, not a hot path.

A multi-year window here means one file download per day (the archive only
publishes daily zips, no monthly bundle). Cached via
``strategies.module.data.factors.get_factor`` — the same DB-backed,
gap-tracked cache as funding/ohlcv, not a local parquet file: a local file
cache doesn't follow the code to a VM/second machine, so every environment
would re-download the whole history from scratch. The DB cache is shared by
whichever machine has ``TIMESCALE_DSN`` pointed at the same database.

Earliest available date verified live 2026-07-22: ~2020-09-01 for BTCUSDT
(binary-searched against the archive; 2020-08-25 = 404, 2020-09-01 = 200).
"""
from __future__ import annotations

import io
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from typing import Callable

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.utils import merge_asof_backward, merge_native_transform_backward

_SOURCE = "data.binance.vision"
_BASE_URL = "https://data.binance.vision/data/futures/um/daily/metrics"

# OI's native cadence is 5 minutes (frequency="M5" below); 288 native periods
# = 24h regardless of the caller's own OHLCV base_tf.
_OI_NATIVE_MINUTES = 5
_OI_24H_PERIODS = 24 * 60 // _OI_NATIVE_MINUTES

# factor_name -> raw column name in the metrics CSV
_COLUMNS = {
    "open_interest": "sum_open_interest",
    "open_interest_value": "sum_open_interest_value",
    "top_trader_ls_account_ratio": "count_toptrader_long_short_ratio",
    "top_trader_ls_position_ratio": "sum_toptrader_long_short_ratio",
    "global_ls_account_ratio": "count_long_short_ratio",
    "taker_buy_sell_ratio": "sum_taker_long_short_vol_ratio",
}


def _fetch_day(symbol_raw: str, day: pd.Timestamp, raw_column: str) -> pd.DataFrame:
    """One day's 5-min metrics file, `raw_column` extracted — or an empty
    frame for a day the archive doesn't have yet (before listing, or not
    yet published for today/yesterday — a 404 here is an expected gap, not
    an error)."""
    date_str = day.strftime("%Y-%m-%d")
    url = f"{_BASE_URL}/{symbol_raw}/{symbol_raw}-metrics-{date_str}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return pd.DataFrame(columns=["timestamp", "value"])
        raise

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        raw = pd.read_csv(io.BytesIO(zf.read(zf.namelist()[0])))
    return pd.DataFrame({
        "timestamp": pd.to_datetime(raw["create_time"], utc=True),
        "value": raw[raw_column].astype(float),
    })


def _metrics_column_fetcher(raw_column: str) -> Callable[[str, datetime, datetime], pd.DataFrame]:
    def _fetch(symbol_raw: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        """One-day-file-per-day fetch over [start_dt, end_dt]. get_factor()
        only calls this for sub-ranges not already cached in the DB, so
        re-running a window that's already covered downloads nothing."""
        start_day = pd.Timestamp(start_dt).normalize()
        # the archive only publishes a day's file after that day is over, so
        # "yesterday" is the newest day that could possibly exist yet
        end_day = min(
            pd.Timestamp(end_dt).normalize(),
            pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1),
        )
        if end_day < start_day:
            return pd.DataFrame(columns=["timestamp", "value"])

        from concurrent.futures import ThreadPoolExecutor

        days = pd.date_range(start_day, end_day, freq="1D")
        def _fetch_one(d):
            return _fetch_day(symbol_raw, d, raw_column)

        with ThreadPoolExecutor(max_workers=16) as executor:
            frames = list(executor.map(_fetch_one, days))

        combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        # data.binance.vision occasionally reports a run of exact-zero
        # values for a few minutes — an upstream reporting outage, not a
        # real reading (none of these metrics legitimately hit exactly 0).
        # Treat as missing and forward-fill (scoped to this fetched window —
        # a zero at the very start of a gap has no anterior value here to
        # fill from, an acceptable edge case since these outages are rare
        # and brief).
        combined.loc[combined["value"] == 0, "value"] = pd.NA
        combined["value"] = combined["value"].ffill()
        return combined

    return _fetch


for _factor_name, _raw_column in _COLUMNS.items():
    register_factor_fetcher(
        _factor_name, _metrics_column_fetcher(_raw_column), source=_SOURCE, frequency="M5",
        # data.binance.vision's futures/um metrics archive is UM-perpetual —
        # same ticker string ("BTCUSDT") as spot, but a different product.
        instrument_type="contract_perpetual",
    )


def fetch_open_interest_history(symbol_raw: str, start: str, end: str) -> pd.DataFrame:
    """DB-cached 5-min open-interest history for `symbol_raw` (no-slash
    perpetual symbol, e.g. ``"BTCUSDT"``) over [start, end], from
    data.binance.vision's public daily archive — not the live 30-day-retention
    REST endpoint (see module docstring).

    Returns columns (timestamp [tz-aware UTC], open_interest). Thin wrapper
    over ``get_factor`` for callers that want the OI-specific column name.
    """
    df = get_factor(symbol_raw, "open_interest", start=start, end=end)
    return df.rename(columns={"value": "open_interest"})


def attach_oi_features(ohlcv: pd.DataFrame, symbol_raw: str, start: str, end: str) -> pd.DataFrame:
    """Merge ``open_interest_change_24h`` onto an OHLCV DataFrame shaped like
    ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp`` column).

    Leaves the column absent entirely (rather than a neutral 0.0 fill) if the
    fetch is empty — callers should treat a missing column as "no OI data",
    not silently assume a value.

    Computed on OI's own native 5-min cadence via ``merge_native_transform_backward``
    (same base_tf-drift pitfall as funding.py's funding_z_3d, see that
    function) — a plain shift on ohlcv's own base_tf would silently mean "24
    bars of the caller's timeframe" instead of 24 real hours.
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    oi = fetch_open_interest_history(symbol_raw, start, end)
    if oi.empty:
        return df

    def _add_change_24h(native: pd.DataFrame) -> pd.DataFrame:
        native["open_interest_change_24h"] = (
            native["open_interest"] / native["open_interest"].shift(_OI_24H_PERIODS) - 1.0
        ) * 100.0
        return native

    return merge_native_transform_backward(df, oi, _add_change_24h)


def attach_positioning_features(ohlcv: pd.DataFrame, symbol_raw: str, start: str, end: str) -> pd.DataFrame:
    """Merge the five long/short positioning factors onto an OHLCV DataFrame
    shaped like ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp``
    column) — everything in ``_COLUMNS`` except ``open_interest`` itself
    (that one has its own ``attach_oi_features``, kept separate since it
    predates this and existing callers already use that name).

    Each column is left absent entirely (not a neutral fill) if that
    factor's fetch is empty — same "missing means missing" convention as
    ``attach_oi_features``.
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    for factor_name in _COLUMNS:
        if factor_name == "open_interest":
            continue
        factor_df = get_factor(symbol_raw, factor_name, start=start, end=end)
        if factor_df.empty:
            continue
        df = merge_asof_backward(df, factor_df.rename(columns={"value": factor_name}))

    return df
