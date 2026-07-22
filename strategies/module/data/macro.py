"""Cross-market macro regime series — FRED-sourced, not tied to any one
exchange or symbol (unlike ``tw_market_flow.py``'s TWSE-specific aggregates).
Feeds regime-style factors for both TW futures and US equity research.

    us_credit_spread     — ICE BofA US High Yield OAS (BAMLH0A0HYM2), daily
    us_financial_conditions — Chicago Fed NFCI, weekly
    us_mortgage_rate      — 30-Year Fixed Rate Mortgage Average (MORTGAGE30US), weekly
    us_vix               — CBOE Volatility Index (VIXCLS), daily
    us_yield_curve_10y2y — 10Y-2Y Treasury yield spread (T10Y2Y), daily —
                            negative means inverted, a standard recession signal
    us_semiconductor_production — Industrial Production: Semiconductor &
                            Other Electronic Component, NAICS 3344 (IPG3344S),
                            monthly — sector supply-side proxy for MU/SOXX
    us_supply_chain_pressure — NY Fed Global Supply Chain Pressure Index
                            (GSCPI, source=nyfed not fred — not on FRED,
                            searched live 2026-07-22, zero hits), monthly

Cached under a fixed pseudo-symbol (``_MACRO_SYMBOL``) — same reasoning as
``tw_market_flow._MARKET_WIDE_SYMBOL``: get_factor's cache key always needs
a symbol, but none of these have a real underlying instrument.

    df = get_factor("MACRO", "us_credit_spread", start="2015-01-01")
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategies.module.data.factors import register_factor_fetcher
from strategies.module.data.providers import fred as fred_client
from strategies.module.data.providers import nyfed as nyfed_client

_SOURCE_FRED = "fred"
_SOURCE_NYFED = "nyfed"
_MACRO_SYMBOL = "MACRO"

# factor_name -> (FRED series_id, update frequency)
_FRED_SERIES = {
    "us_credit_spread": ("BAMLH0A0HYM2", "D1"),
    "us_financial_conditions": ("NFCI", "W1"),
    "us_mortgage_rate": ("MORTGAGE30US", "W1"),
    "us_vix": ("VIXCLS", "D1"),
    "us_yield_curve_10y2y": ("T10Y2Y", "D1"),
    "us_semiconductor_production": ("IPG3344S", "MN1"),
}


def _fred_series_fetcher(series_id: str):
    def _fetch(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        rows = fred_client.fetch(series_id, start_dt, end_dt)
        # FRED marks no-release days with value="." — drop, not 0-fill.
        records = [
            {"timestamp": pd.Timestamp(r["date"], tz="UTC"), "value": float(r["value"])}
            for r in rows if r["value"] != "."
        ]
        if not records:
            return pd.DataFrame(columns=["timestamp", "value"])
        return pd.DataFrame(records).sort_values("timestamp").reset_index(drop=True)

    return _fetch


def _gscpi_fetcher(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    # nyfed's endpoint has no date-range param — fetches full history every
    # call, filtered here. Fine at monthly granularity (a few hundred rows).
    df = nyfed_client.fetch()
    df["timestamp"] = pd.to_datetime(df["date"], utc=True)
    df = df[(df["timestamp"] >= start_dt) & (df["timestamp"] <= end_dt)]
    return df[["timestamp", "value"]].sort_values("timestamp").reset_index(drop=True)


for _factor_name, (_series_id, _frequency) in _FRED_SERIES.items():
    register_factor_fetcher(_factor_name, _fred_series_fetcher(_series_id), source=_SOURCE_FRED, frequency=_frequency, instrument_type="spot")

register_factor_fetcher("us_supply_chain_pressure", _gscpi_fetcher, source=_SOURCE_NYFED, frequency="MN1", instrument_type="spot")
