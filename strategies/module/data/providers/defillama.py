"""Thin DefiLlama stablecoins client — one function, no business logic.

No API key, no rate limit on this endpoint (verified live 2026-07-22).
"""
from __future__ import annotations

import json
import urllib.request

import pandas as pd

_STABLECOIN_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
_TVL_URL = "https://api.llama.fi/v2/historicalChainTvl"


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_stablecoin_mcap() -> pd.DataFrame:
    """Full daily history of aggregate USD-pegged stablecoin market cap —
    columns ``[date, value]``, `date` UTC ``Timestamp`` (day resolution).

    No date-range param on this endpoint — always full history (from
    2017-11-29), caller slices by date. Only peggedUSD is extracted; the
    response also bundles other peg currencies (peggedEUR, ...), not
    relevant to a crypto liquidity signal.
    """
    raw = _get(_STABLECOIN_URL)
    records = [
        {"date": pd.Timestamp(int(row["date"]), unit="s", tz="UTC"), "value": row["totalCirculatingUSD"]["peggedUSD"]}
        for row in raw
        if "peggedUSD" in row.get("totalCirculatingUSD", {})
    ]
    return pd.DataFrame(records).sort_values("date").reset_index(drop=True)


def fetch_defi_tvl() -> pd.DataFrame:
    """Full daily history of total DeFi TVL across all chains — columns
    ``[date, value]``, `value` in USD. No date-range param, full history
    (from 2017-09) filtered by caller.
    """
    raw = _get(_TVL_URL)
    records = [{"date": pd.Timestamp(int(row["date"]), unit="s", tz="UTC"), "value": row["tvl"]} for row in raw]
    return pd.DataFrame(records).sort_values("date").reset_index(drop=True)
