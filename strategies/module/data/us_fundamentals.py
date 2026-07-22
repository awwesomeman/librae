"""US-listed single-stock fundamentals — SEC EDGAR XBRL company facts.

Quarterly only (fp in Q1-Q4, annual/FY skipped — different cadence, own
factor_name later if needed). ts = filing's `filed` date, not period `end`
— numbers aren't public until filed, so `end` would leak future data into
a backtest (same point-in-time convention as every other factor).

XBRL facts come in two shapes (SEC's own terms; = flow/stock in accounting):
    duration (flow)  — accumulated over a period (start, end both set):
                        revenue, income, cash flow items
    instant  (stock) — a snapshot at one date (end only, no start):
                        balance sheet items (inventory, shares outstanding, ...)
Both go through the same dedup/point-in-time logic, just keyed differently.

    df = get_factor("MU", "us_revenue", start="2024-01-01")
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.providers import sec_edgar

logger = logging.getLogger(__name__)

_SOURCE = "sec_edgar"

# factor_name -> (us-gaap concept, unit key)
# MU tags revenue under RevenueFromContractWithCustomerExcludingAssessedTax,
# not the generic Revenues (empty for this filer) — check per new ticker.
_DURATION_CONCEPTS = {
    "us_revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
    "us_net_income": ("NetIncomeLoss", "USD"),
    "us_eps_diluted": ("EarningsPerShareDiluted", "USD/shares"),
    "us_gross_profit": ("GrossProfit", "USD"),
    "us_operating_income": ("OperatingIncomeLoss", "USD"),
    "us_operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities", "USD"),
    "us_capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
}

# Balance-sheet snapshots — no period to accumulate over, just a date.
_INSTANT_CONCEPTS = {
    "us_inventory": ("InventoryNet", "USD"),
    "us_shares_outstanding": ("CommonStockSharesOutstanding", "shares"),
}

_QUARTER_DAYS_MIN, _QUARTER_DAYS_MAX = 80, 100


def _first_disclosure(entries: list[dict], key_fn: Callable[[dict], object], label: str, symbol: str) -> dict:
    """Earliest `filed` entry per key_fn(entry); warns if a later filing
    re-reports the same key with a different value (see module docstring
    — SEC gives no restated/original flag, this is the only signal)."""
    first: dict = {}
    for e in entries:
        key = key_fn(e)
        prior = first.get(key)
        if prior is not None and e["val"] != prior["val"]:
            logger.warning(
                "%s %s: %s re-reported with a different value (%.4g on %s vs %.4g on %s) — using the earlier filing",
                symbol, label, key, prior["val"], prior["filed"], e["val"], e["filed"],
            )
        if prior is None or e["filed"] < prior["filed"]:
            first[key] = e
    return first


def _entries_to_df(first_disclosure: dict, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    if not first_disclosure:
        return pd.DataFrame(columns=["timestamp", "value"])
    df = pd.DataFrame(
        {"timestamp": pd.Timestamp(e["filed"], tz="UTC"), "value": float(e["val"])}
        for e in first_disclosure.values()
    ).sort_values("timestamp").reset_index(drop=True)
    return df[(df["timestamp"] >= start_dt) & (df["timestamp"] <= end_dt)].reset_index(drop=True)


def _quarterly_duration_fetcher(concept: str, unit: str) -> Callable[[str, datetime, datetime], pd.DataFrame]:
    def _fetch(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        facts = sec_edgar.fetch_company_facts(symbol)
        entries = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get(unit, [])
        # XBRL tags both the single quarter and the YTD-cumulative figure
        # under the same fp — filter to ~quarter-length duration only.
        quarterly = [
            e for e in entries
            if e.get("fp") in ("Q1", "Q2", "Q3", "Q4")
            and _QUARTER_DAYS_MIN <= (pd.Timestamp(e["end"]) - pd.Timestamp(e["start"])).days <= _QUARTER_DAYS_MAX
        ]
        first_disclosure = _first_disclosure(quarterly, lambda e: (e["start"], e["end"]), concept, symbol)
        return _entries_to_df(first_disclosure, start_dt, end_dt)

    return _fetch


def _quarterly_instant_fetcher(concept: str, unit: str) -> Callable[[str, datetime, datetime], pd.DataFrame]:
    def _fetch(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        facts = sec_edgar.fetch_company_facts(symbol)
        entries = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {}).get(unit, [])
        quarterly = [e for e in entries if e.get("fp") in ("Q1", "Q2", "Q3", "Q4")]
        first_disclosure = _first_disclosure(quarterly, lambda e: e["end"], concept, symbol)
        return _entries_to_df(first_disclosure, start_dt, end_dt)

    return _fetch


for _factor_name, (_concept, _unit) in _DURATION_CONCEPTS.items():
    register_factor_fetcher(
        _factor_name, _quarterly_duration_fetcher(_concept, _unit),
        source=_SOURCE, frequency="MN3", instrument_type="spot",
    )

for _factor_name, (_concept, _unit) in _INSTANT_CONCEPTS.items():
    register_factor_fetcher(
        _factor_name, _quarterly_instant_fetcher(_concept, _unit),
        source=_SOURCE, frequency="MN3", instrument_type="spot",
    )
