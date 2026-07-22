"""US-listed single-stock "chip" data — FINRA-sourced short positioning +
turnover (a computed feature, not a raw factor — see below).

    us_short_interest     — bi-weekly settlement short position (shares),
                             ConsolidatedShortInterest dataset
    us_short_volume_ratio — daily short-sale volume / total volume, summed
                             across all 3 TRFs (NYSE/Nasdaq/ADF), regShoDaily

Previously sourced from yfinance's short-interest snapshot fields, which
only ever return the current settlement + one prior month (never a
backfillable range) — that forced an append-only, coverage-tracking-free
design (see git history). FINRA's own ConsolidatedShortInterest is the
primary source yfinance's numbers ultimately come from anyway, and *does*
support arbitrary date-range queries — so this now goes through the normal
get_factor()/register_factor_fetcher() path like every other factor here.
No FINRA API key needed (public data API).

Both factors are at their native reporting frequency already — short
interest is only computed bi-weekly at settlement (exchange-side, not an
API limitation), daily short volume is FINRA's finest granularity (no
intraday breakdown available).

    df = get_factor("MU", "us_short_interest", start="2015-01-01")
    df = get_factor("MU", "us_short_volume_ratio", start="2024-01-01")
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import pandas as pd

from strategies.module.data import us_fundamentals  # noqa: F401  (registers us_shares_outstanding fetcher)
from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.providers import finra as finra_client
from strategies.module.data.utils import attach_or_zero_fill

_SOURCE = "finra"
_GROUP = "otcMarket"


def _short_interest_fetcher(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    body = {
        "compareFilters": [{"fieldName": "symbolCode", "fieldValue": symbol, "compareType": "EQUAL"}],
        "dateRangeFilters": [{
            "fieldName": "settlementDate",
            "startDate": start_dt.date().isoformat(), "endDate": end_dt.date().isoformat(),
        }],
    }
    rows = finra_client.fetch(_GROUP, "consolidatedShortInterest", body)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "value"])
    df = pd.DataFrame(
        {"timestamp": pd.Timestamp(r["settlementDate"], tz="UTC"), "value": float(r["currentShortPositionQuantity"])}
        for r in rows
    )
    return df.sort_values("timestamp").reset_index(drop=True)


def _short_volume_ratio_fetcher(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    body = {
        "compareFilters": [{
            "fieldName": "securitiesInformationProcessorSymbolIdentifier", "fieldValue": symbol, "compareType": "EQUAL",
        }],
        "dateRangeFilters": [{
            "fieldName": "tradeReportDate",
            "startDate": start_dt.date().isoformat(), "endDate": end_dt.date().isoformat(),
        }],
    }
    rows = finra_client.fetch(_GROUP, "regShoDaily", body)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "value"])

    # One row per TRF (NYSE/Nasdaq/ADF) per day — sum across facilities
    # before computing the ratio, a single TRF's split isn't meaningful alone.
    short_by_date: dict[str, float] = defaultdict(float)
    total_by_date: dict[str, float] = defaultdict(float)
    for r in rows:
        d = r["tradeReportDate"]
        short_by_date[d] += float(r["shortParQuantity"])
        total_by_date[d] += float(r["totalParQuantity"])

    df = pd.DataFrame(
        {"timestamp": pd.Timestamp(d, tz="UTC"), "value": short_by_date[d] / total_by_date[d]}
        for d in short_by_date if total_by_date[d] > 0
    )
    return df.sort_values("timestamp").reset_index(drop=True)


register_factor_fetcher("us_short_interest", _short_interest_fetcher, source=_SOURCE, frequency="W2", instrument_type="spot")
register_factor_fetcher("us_short_volume_ratio", _short_volume_ratio_fetcher, source=_SOURCE, frequency="D1", instrument_type="spot")


def attach_turnover_rate(ohlcv: pd.DataFrame, symbol: str, start: str, end: str) -> pd.DataFrame:
    """Attach ``turnover_rate`` (daily volume / shares outstanding) to an
    OHLCV DataFrame shaped like ``data.ohlcv.get_ohlcv()``'s output.

    Computed on the fly, not a registered factor — no extra API call needed,
    it's ``ohlcv.volume`` (already cached) divided by
    ``us_shares_outstanding`` (SEC EDGAR, already cached by
    ``us_fundamentals.py``), backward-asof merged since shares outstanding
    is only reported quarterly. 0.0 where shares-outstanding data isn't
    available yet (same "no data yet" convention as every other
    attach_*_features here — see ``utils.attach_or_zero_fill``).

    Uses SEC's total shares outstanding, not free float — free float
    (excludes insider/restricted shares) is the more precise turnover
    denominator but only available as a live yfinance snapshot
    (``floatShares``), not a point-in-time historical series; using the
    point-in-time-correct total avoids a backtest look-ahead trade-off for
    a difference that's usually a few percent.
    """
    shares_outstanding = get_factor(symbol, "us_shares_outstanding", start=start, end=end)
    df = attach_or_zero_fill(ohlcv, "shares_outstanding", shares_outstanding, "value")
    df["turnover_rate"] = (df["volume"] / df["shares_outstanding"]).where(df["shares_outstanding"] > 0, 0.0)
    return df.drop(columns=["shares_outstanding"])
