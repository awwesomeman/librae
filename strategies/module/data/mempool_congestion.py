"""Bitcoin mempool congestion — mempool.space-sourced on-chain demand proxy.
A backlog spike means blockspace demand is outrunning supply (often
coincides with volatility/urgency — exchange inflows, panic selling); a
near-empty mempool means quiet on-chain demand.

Keyed under the bare asset symbol ``"BTC"``, same as ``hashrate.py``.

    df = get_factor("BTC", "btc_mempool_tx_count", start="2020-01-01")
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.hashrate import _BTC_SYMBOL
from strategies.module.data.providers import mempool_space as mempool_client
from strategies.module.data.utils import merge_asof_backward

_SOURCE = "mempool.space"


def _fetch_mempool_tx_count(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    # No date-range param on the source endpoint — fetches full history
    # every call, filtered here (~7000 rows as of 2026-07-22).
    df = mempool_client.fetch_mempool_stats()
    df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
    return df.rename(columns={"date": "timestamp"})[["timestamp", "value"]].reset_index(drop=True)


register_factor_fetcher(
    "btc_mempool_tx_count", _fetch_mempool_tx_count, source=_SOURCE, frequency="H12", instrument_type="spot",
)


def fetch_mempool_tx_count_history(start: str, end: str) -> pd.DataFrame:
    """DB-cached mempool-backlog-count history over [start, end]."""
    df = get_factor(_BTC_SYMBOL, "btc_mempool_tx_count", start=start, end=end)
    return df.rename(columns={"value": "btc_mempool_tx_count"})


def attach_mempool_congestion_features(ohlcv: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Merge btc_mempool_tx_count onto an OHLCV DataFrame shaped like
    ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp`` column). No
    `symbol` argument — always Bitcoin network-level. 0.0 if no data is
    available yet.
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    congestion = fetch_mempool_tx_count_history(start, end)
    if congestion.empty:
        df["btc_mempool_tx_count"] = 0.0
        return df

    df = merge_asof_backward(df, congestion)
    df["btc_mempool_tx_count"] = df["btc_mempool_tx_count"].fillna(0.0)
    return df
