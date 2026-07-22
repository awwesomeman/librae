"""Historical open-interest for crypto perpetuals.

Binance's live OI-history REST endpoint (``fetch_open_interest_history`` via
ccxt, or ``/futures/data/openInterestHist`` directly) only retains ~30 days —
a `since` outside that window is a hard 400 from the exchange, too short for
multi-month backtest windows. ``data.binance.vision`` publishes the same
``sum_open_interest`` metric as a public, no-auth daily bulk archive with no
such retention limit.

Unlike funding rate (~700 rows via a single paginated API call, cheap enough
to refetch every run — see data/funding.py), a multi-year window here means
one file download per day (the archive only publishes daily zips, no monthly
bundle for this metric). Cached via ``strategies.module.data.factors.get_factor``
(factor_name='open_interest') — the same DB-backed, gap-tracked cache as
funding/ohlcv, not a local parquet file: a local file cache doesn't follow
the code to a VM/second machine, so every environment would re-download the
whole history from scratch. The DB cache is shared by whichever machine has
``TIMESCALE_DSN`` pointed at the same database.
"""
from __future__ import annotations

import io
import urllib.error
import urllib.request
import zipfile
from datetime import datetime

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.utils import merge_asof_backward

_SOURCE = "data.binance.vision"
_BASE_URL = "https://data.binance.vision/data/futures/um/daily/metrics"


def _fetch_day(symbol_raw: str, day: pd.Timestamp) -> pd.DataFrame:
    """One day's 5-min open-interest file, or an empty frame for a day the
    archive doesn't have yet (before listing, or not yet published for
    today/yesterday — a 404 here is an expected gap, not an error)."""
    date_str = day.strftime("%Y-%m-%d")
    url = f"{_BASE_URL}/{symbol_raw}/{symbol_raw}-metrics-{date_str}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return pd.DataFrame({
                "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
                "open_interest": pd.Series(dtype="float64"),
            })
        raise

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        raw = pd.read_csv(io.BytesIO(zf.read(zf.namelist()[0])))
    return pd.DataFrame({
        "timestamp": pd.to_datetime(raw["create_time"], utc=True),
        "open_interest": raw["sum_open_interest"].astype(float),
    })


def _fetch_oi_range(symbol_raw: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """One-day-file-per-day fetch over [start_dt, end_dt]. Returns columns
    (timestamp [tz-aware UTC], value) — get_factor() only calls this for
    sub-ranges not already cached in the DB, so re-running a window that's
    already covered downloads nothing."""
    start_day = pd.Timestamp(start_dt).normalize()
    # the archive only publishes a day's file after that day is over, so
    # "yesterday" is the newest day that could possibly exist yet
    end_day = min(
        pd.Timestamp(end_dt).normalize(),
        pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1),
    )
    if end_day < start_day:
        return pd.DataFrame(columns=["timestamp", "value"])

    frames = []
    day = start_day
    while day <= end_day:
        frames.append(_fetch_day(symbol_raw, day))
        day += pd.Timedelta(days=1)

    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    # data.binance.vision occasionally reports a run of exact-zero
    # sum_open_interest for a few minutes — an upstream reporting outage, not
    # a real reading (OI doesn't legitimately hit exactly 0). Treat as
    # missing and forward-fill (scoped to this fetched window — a zero at
    # the very start of a gap has no anterior value here to fill from,
    # an acceptable edge case since these outages are rare and brief).
    combined.loc[combined["open_interest"] == 0, "open_interest"] = pd.NA
    combined["open_interest"] = combined["open_interest"].ffill()
    return combined.rename(columns={"open_interest": "value"})


register_factor_fetcher(
    "open_interest", _fetch_oi_range, source=_SOURCE,
    # data.binance.vision's futures/um metrics archive is UM-perpetual OI —
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
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    oi = fetch_open_interest_history(symbol_raw, start, end)
    if oi.empty:
        return df

    df = merge_asof_backward(df, oi)
    df["open_interest_change_24h"] = (df["open_interest"] / df["open_interest"].shift(24) - 1.0) * 100.0
    return df
