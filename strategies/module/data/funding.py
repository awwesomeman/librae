"""Perpetual-futures funding-rate history — the closest publicly-available
proxy for crypto "chip"/positioning data: funding rate is the fee leveraged
longs/shorts pay each other every 8h to keep the perpetual price anchored to
spot, so persistently positive funding means the perp order book is crowded
long (and vice versa) — a real, continuously-updated positioning signal, not
something inferred from price alone.

Public ccxt endpoint, no auth. Cached in TimescaleDB via
``strategies.module.data.factors.get_factor`` (factor_name='funding_rate') — same
DB-first + gap-tracked cache as ohlcv, so a re-run on a different machine
(e.g. the VM) doesn't need to re-download anything already fetched
elsewhere; a multi-year history is only ~700 rows either way, so refetching
an uncached range is cheap.

Binance's live open-interest-history endpoint only retains ~30 days; see
data/open_interest.py for the archive-based alternative with full history.

``basis_premium`` — perp-vs-spot premium index (%), the raw input funding
rate is derived/clamped from. Same problem domain as funding_rate, so it
lives here rather than a new file, even though the source archive
(data.binance.vision's ``premiumIndexKlines``) is OHLC-per-interval — only
the close is kept, get_factor()'s schema has no room for a full OHLC row.
Unlike OI/ratio metrics elsewhere, an exact-0 premium is a real reading
(perp trading exactly at spot), not an outage — no zero-forward-fill here.

Uses the raw no-slash symbol (``"BTCUSDT"``, matching open_interest.py),
not funding_rate's ccxt slashed format (``"BTC/USDT:USDT"``) — this fetcher
doesn't go through ccxt at all.
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

_EXCHANGE_ID = "binanceusdm"
_PREMIUM_SOURCE = "data.binance.vision"
_PREMIUM_BASE_URL = "https://data.binance.vision/data/futures/um/daily/premiumIndexKlines"
_PREMIUM_INTERVAL = "1h"


def _fetch_funding_rate_page(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """Paginated funding-rate history for `symbol` (ccxt perpetual symbol,
    e.g. ``"BTC/USDT:USDT"``) over [start_dt, end_dt].

    Returns columns (timestamp [tz-aware UTC], value) — raw per-8h rate, one
    row per funding event (~3/day on Binance).
    """
    import ccxt

    exchange = getattr(ccxt, _EXCHANGE_ID)({"enableRateLimit": True})
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    rows: list = []
    since = start_ms
    while since <= end_ms:
        page = exchange.fetch_funding_rate_history(symbol, since=since, limit=1000)
        if not page:
            break
        rows.extend(page)
        next_since = page[-1]["timestamp"] + 1
        if next_since <= since:
            break  # no progress; avoid looping forever
        since = next_since
        if len(page) < 1000:
            break  # short page means we've caught up to the exchange's latest print

    records = [
        {"timestamp": pd.to_datetime(r["timestamp"], unit="ms", utc=True), "value": r["fundingRate"]}
        for r in rows
        if start_ms <= r["timestamp"] <= end_ms
    ]
    if not records:
        return pd.DataFrame(columns=["timestamp", "value"])
    return (
        pd.DataFrame(records)
        .drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


register_factor_fetcher(
    "funding_rate", _fetch_funding_rate_page, source=_EXCHANGE_ID, frequency="H8",
    instrument_type="contract_perpetual",  # funding rate only exists for perpetuals
)


def _fetch_premium_day(symbol_raw: str, day: pd.Timestamp) -> pd.DataFrame:
    """One day's hourly premium-index file, or an empty frame for a day the
    archive doesn't have yet (a 404 here is an expected gap, not an error)."""
    date_str = day.strftime("%Y-%m-%d")
    url = f"{_PREMIUM_BASE_URL}/{symbol_raw}/{_PREMIUM_INTERVAL}/{symbol_raw}-{_PREMIUM_INTERVAL}-{date_str}.zip"
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
        "timestamp": pd.to_datetime(raw["open_time"], unit="ms", utc=True),
        "value": raw["close"].astype(float),
    })


def _fetch_basis_premium(symbol_raw: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """One-day-file-per-day fetch over [start_dt, end_dt] — same pagination
    shape as open_interest.py's fetcher (get_factor() only calls this for
    sub-ranges not already cached)."""
    start_day = pd.Timestamp(start_dt).normalize()
    end_day = min(
        pd.Timestamp(end_dt).normalize(),
        pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1),
    )
    if end_day < start_day:
        return pd.DataFrame(columns=["timestamp", "value"])

    frames = []
    day = start_day
    while day <= end_day:
        frames.append(_fetch_premium_day(symbol_raw, day))
        day += pd.Timedelta(days=1)

    return pd.concat(frames, ignore_index=True).drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)


register_factor_fetcher(
    "basis_premium", _fetch_basis_premium, source=_PREMIUM_SOURCE, frequency="H1",
    instrument_type="contract_perpetual",
)


def fetch_funding_rate_history(symbol: str, start: str, end: str) -> pd.DataFrame:
    """DB-cached funding-rate history for `symbol` (ccxt perpetual symbol,
    e.g. ``"BTC/USDT:USDT"``) over [start, end].

    Returns columns (timestamp [tz-aware UTC], funding_rate) — raw per-8h
    rate, one row per funding event (~3/day on Binance). Thin wrapper over
    ``get_factor`` for callers that want the funding-specific column name.
    """
    df = get_factor(symbol, "funding_rate", start=start, end=end)
    return df.rename(columns={"value": "funding_rate"})


def attach_funding_features(ohlcv: pd.DataFrame, symbol: str, start: str, end: str) -> pd.DataFrame:
    """Merge funding-rate-derived factors onto an OHLCV DataFrame shaped like
    ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp`` column).

    Funding events happen every 8h (00:00/08:00/16:00 UTC on Binance); each
    realized rate becomes public exactly at its ``fundingTime``, so a
    backward asof-merge (each bar only sees the most recent already-realized
    funding print) carries no look-ahead.

    Adds:
        funding_rate:   raw per-8h rate, forward-filled from the last print.
        funding_z_3d:   rolling z-score over ~3 days of prints (9 events) —
                        "how extreme is crowding right now vs its own recent range".
        funding_cum_3:  cumulative last-3-prints funding — "has crowding
                        been sustained", not just one extreme print.

    All three are 0.0 if no funding data is available for `symbol`.
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    funding = fetch_funding_rate_history(symbol, start, end)
    if funding.empty:
        df["funding_rate"] = 0.0
        df["funding_z_3d"] = 0.0
        df["funding_cum_3"] = 0.0
        return df

    df = merge_asof_backward(df, funding)
    df["funding_rate"] = df["funding_rate"].fillna(0.0)

    roll = df["funding_rate"].rolling(9, min_periods=9)
    df["funding_z_3d"] = ((df["funding_rate"] - roll.mean()) / (roll.std() + 1e-9)).fillna(0.0)
    df["funding_cum_3"] = df["funding_rate"].rolling(3, min_periods=3).sum().fillna(0.0)
    return df
