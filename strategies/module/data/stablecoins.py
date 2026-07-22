"""Stablecoin market cap — DefiLlama-sourced macro liquidity proxy. Rising
supply = capital entering crypto, falling = net redemption.

Market-wide, not tied to any symbol — same pseudo-symbol pattern as
``tw_market_flow.py``/``macro.py`` (get_factor's cache key needs a symbol,
this factor has no underlying instrument).

    df = get_factor("CRYPTO_MARKET", "stablecoin_mcap_total", start="2020-01-01")
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.providers import defillama as defillama_client
from strategies.module.data.utils import merge_asof_backward

_SOURCE = "defillama"
_MARKET_WIDE_SYMBOL = "CRYPTO_MARKET"


def _fetch_stablecoin_mcap(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    # defillama_client.fetch_stablecoin_mcap() has no date-range param —
    # fetches full history every call, filtered here. Fine at daily
    # granularity (~3200 rows total as of 2026-07-22).
    df = defillama_client.fetch_stablecoin_mcap()
    df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
    return df.rename(columns={"date": "timestamp"})[["timestamp", "value"]].reset_index(drop=True)


register_factor_fetcher(
    "stablecoin_mcap_total", _fetch_stablecoin_mcap, source=_SOURCE, frequency="D1", instrument_type="spot",
)


def fetch_stablecoin_mcap_history(start: str, end: str) -> pd.DataFrame:
    """DB-cached daily aggregate USD-pegged stablecoin market cap over [start, end]."""
    df = get_factor(_MARKET_WIDE_SYMBOL, "stablecoin_mcap_total", start=start, end=end)
    return df.rename(columns={"value": "stablecoin_mcap_total"})


def attach_stablecoin_features(ohlcv: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Merge stablecoin-supply-derived factors onto an OHLCV DataFrame shaped
    like ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp`` column).

    No `symbol` argument — market-wide (same as
    ``tw_market_flow.attach_tw_market_flow_features``).

    Adds stablecoin_mcap_total (raw level) and stablecoin_mcap_chg_7d
    (7-day % change). Both 0.0 if no data is available yet.
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    mcap = fetch_stablecoin_mcap_history(start, end)
    if mcap.empty:
        df["stablecoin_mcap_total"] = 0.0
        df["stablecoin_mcap_chg_7d"] = 0.0
        return df

    df = merge_asof_backward(df, mcap)
    df["stablecoin_mcap_total"] = df["stablecoin_mcap_total"].fillna(0.0)

    chg_7d = mcap["stablecoin_mcap_total"].pct_change(7) * 100.0
    chg_series = mcap[["timestamp"]].assign(stablecoin_mcap_chg_7d=chg_7d)
    df = merge_asof_backward(df, chg_series)
    df["stablecoin_mcap_chg_7d"] = df["stablecoin_mcap_chg_7d"].fillna(0.0)
    return df
