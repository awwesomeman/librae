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
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.utils import merge_asof_backward

_EXCHANGE_ID = "binanceusdm"


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
    "funding_rate", _fetch_funding_rate_page, source=_EXCHANGE_ID,
    instrument_type="contract_perpetual",  # funding rate only exists for perpetuals
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
