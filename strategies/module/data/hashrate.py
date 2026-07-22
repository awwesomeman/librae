"""Bitcoin network hashrate/difficulty — mempool.space-sourced miner-confidence
proxy. Sustained hashrate growth = miners committing more compute (bullish
conviction); a sharp drop = capitulation (miners switching off unprofitable
rigs, historically a bear-bottom marker).

Keyed under the bare asset symbol ``"BTC"`` (network-level data, not tied to
any trading pair or exchange) — distinct from OHLCV/funding/OI's
``"BTCUSDT"``, which is Binance's pair ticker.

    df = get_factor("BTC", "btc_hashrate", start="2020-01-01")
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategies.module.data.factors import FREQUENCY_IRREGULAR, get_factor, register_factor_fetcher
from strategies.module.data.providers import mempool_space as mempool_client
from strategies.module.data.utils import merge_asof_backward

_SOURCE = "mempool.space"
_BTC_SYMBOL = "BTC"


def _fetch_hashrate(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    # No date-range param on the source endpoint — fetches full history
    # every call, filtered here (~6400 daily rows as of 2026-07-22).
    df = mempool_client.fetch_hashrate()
    df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
    return df.rename(columns={"date": "timestamp"})[["timestamp", "value"]].reset_index(drop=True)


def _fetch_difficulty(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    # ~2-week retarget events, not a fixed grid — irregular frequency.
    df = mempool_client.fetch_difficulty_adjustments()
    df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
    return df.rename(columns={"date": "timestamp"})[["timestamp", "value"]].reset_index(drop=True)


register_factor_fetcher("btc_hashrate", _fetch_hashrate, source=_SOURCE, frequency="D1", instrument_type="spot")
register_factor_fetcher("btc_difficulty", _fetch_difficulty, source=_SOURCE, frequency=FREQUENCY_IRREGULAR, instrument_type="spot")


def fetch_hashrate_history(start: str, end: str) -> pd.DataFrame:
    """DB-cached daily network-hashrate history over [start, end]."""
    df = get_factor(_BTC_SYMBOL, "btc_hashrate", start=start, end=end)
    return df.rename(columns={"value": "btc_hashrate"})


def fetch_difficulty_history(start: str, end: str) -> pd.DataFrame:
    """DB-cached difficulty-retarget history over [start, end]."""
    df = get_factor(_BTC_SYMBOL, "btc_difficulty", start=start, end=end)
    return df.rename(columns={"value": "btc_difficulty"})


def attach_hashrate_features(ohlcv: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Merge hashrate/difficulty-derived factors onto an OHLCV DataFrame
    shaped like ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp``
    column). No `symbol` argument — always Bitcoin network-level.

    Adds btc_hashrate (raw level), btc_hashrate_chg_30d (30-day % change —
    miner-confidence trend), and btc_difficulty (raw level, ffilled between
    retarget events). All 0.0 if no data is available yet.
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    hashrate = fetch_hashrate_history(start, end)
    if hashrate.empty:
        df["btc_hashrate"] = 0.0
        df["btc_hashrate_chg_30d"] = 0.0
    else:
        df = merge_asof_backward(df, hashrate)
        df["btc_hashrate"] = df["btc_hashrate"].fillna(0.0)
        chg_30d = hashrate["btc_hashrate"].pct_change(30) * 100.0
        chg_series = hashrate[["timestamp"]].assign(btc_hashrate_chg_30d=chg_30d)
        df = merge_asof_backward(df, chg_series)
        df["btc_hashrate_chg_30d"] = df["btc_hashrate_chg_30d"].fillna(0.0)

    difficulty = fetch_difficulty_history(start, end)
    if difficulty.empty:
        df["btc_difficulty"] = 0.0
    else:
        df = merge_asof_backward(df, difficulty)
        df["btc_difficulty"] = df["btc_difficulty"].fillna(0.0)

    return df
