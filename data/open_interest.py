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
bundle for this metric), so this module caches fetched days to a local
parquet file per symbol instead of re-downloading every run.
"""
from __future__ import annotations

import io
import os
import urllib.error
import urllib.request
import zipfile

import pandas as pd

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache", "open_interest")
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


def fetch_open_interest_history(symbol_raw: str, start: str, end: str) -> pd.DataFrame:
    """5-min open-interest history for `symbol_raw` (no-slash perpetual
    symbol, e.g. ``"BTCUSDT"``) over [start, end] (date strings, UTC), from
    data.binance.vision's public daily archive — not the live 30-day-retention
    REST endpoint (see module docstring).

    Cached to a local parquet file per symbol (``data/cache/open_interest/``)
    so re-running the same window only downloads days not already on disk.
    Returns columns (timestamp [tz-aware UTC], open_interest).
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(_CACHE_DIR, f"{symbol_raw}.parquet")

    if os.path.exists(cache_path):
        cached = pd.read_parquet(cache_path)
        cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True)
    else:
        cached = pd.DataFrame({
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "open_interest": pd.Series(dtype="float64"),
        })

    start_day = pd.Timestamp(start, tz="UTC").normalize()
    # the archive only publishes a day's file after that day is over, so
    # "yesterday" is the newest day that could possibly exist yet
    end_day = min(
        pd.Timestamp(end, tz="UTC").normalize(),
        pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1),
    )
    cached_days = set(cached["timestamp"].dt.normalize().unique())

    new_frames = []
    day = start_day
    while day <= end_day:
        if day not in cached_days:
            new_frames.append(_fetch_day(symbol_raw, day))
        day += pd.Timedelta(days=1)

    if new_frames:
        combined = pd.concat([cached] + new_frames, ignore_index=True)
        combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        # data.binance.vision occasionally reports a run of exact-zero
        # sum_open_interest for a few minutes — an upstream reporting outage,
        # not a real reading (OI doesn't legitimately hit exactly 0). Treat
        # as missing and forward-fill rather than feeding a fake -100% swing
        # into open_interest_change_24h.
        combined.loc[combined["open_interest"] == 0, "open_interest"] = pd.NA
        combined["open_interest"] = combined["open_interest"].ffill()
        combined.to_parquet(cache_path, index=False)
    else:
        combined = cached

    mask = (combined["timestamp"] >= pd.Timestamp(start, tz="UTC")) & (combined["timestamp"] <= pd.Timestamp(end, tz="UTC"))
    return combined.loc[mask].sort_values("timestamp").reset_index(drop=True)


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

    df = df.sort_values("timestamp")
    oi = oi.sort_values("timestamp")
    df["timestamp"] = df["timestamp"].astype("datetime64[ns, UTC]")
    oi["timestamp"] = oi["timestamp"].astype("datetime64[ns, UTC]")
    df = pd.merge_asof(df, oi, on="timestamp", direction="backward")
    df["open_interest_change_24h"] = (df["open_interest"] / df["open_interest"].shift(24) - 1.0) * 100.0
    return df
