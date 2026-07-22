"""DeFi total-value-locked — DefiLlama-sourced risk-appetite proxy. Rising
TVL means capital is willing to lock up (not just park) inside crypto;
falling TVL is de-risking, distinct from ``stablecoins.py``'s "how much
dollar liquidity exists at all" signal.

Market-wide, same pseudo-symbol as ``stablecoins.py`` (both answer "how much
capital is in the crypto ecosystem", just different slices of it).

    df = get_factor("CRYPTO_MARKET", "defi_tvl_total", start="2020-01-01")
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.providers import defillama as defillama_client
from strategies.module.data.stablecoins import _MARKET_WIDE_SYMBOL
from strategies.module.data.utils import merge_asof_backward

_SOURCE = "defillama"


def _fetch_defi_tvl(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    # No date-range param on the source endpoint — fetches full history
    # every call, filtered here (~3200 rows as of 2026-07-22).
    df = defillama_client.fetch_defi_tvl()
    df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
    return df.rename(columns={"date": "timestamp"})[["timestamp", "value"]].reset_index(drop=True)


register_factor_fetcher(
    "defi_tvl_total", _fetch_defi_tvl, source=_SOURCE, frequency="D1", instrument_type="spot",
)


def fetch_defi_tvl_history(start: str, end: str) -> pd.DataFrame:
    """DB-cached daily total-DeFi-TVL history over [start, end]."""
    df = get_factor(_MARKET_WIDE_SYMBOL, "defi_tvl_total", start=start, end=end)
    return df.rename(columns={"value": "defi_tvl_total"})


def attach_defi_tvl_features(ohlcv: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Merge defi_tvl_total onto an OHLCV DataFrame shaped like
    ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp`` column). No
    `symbol` argument — market-wide. 0.0 if no data is available yet.
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    tvl = fetch_defi_tvl_history(start, end)
    if tvl.empty:
        df["defi_tvl_total"] = 0.0
        return df

    df = merge_asof_backward(df, tvl)
    df["defi_tvl_total"] = df["defi_tvl_total"].fillna(0.0)
    return df
