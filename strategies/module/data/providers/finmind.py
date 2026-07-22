"""Thin FinMind REST client — one function, no business logic.

Shared by every FinMind-backed factor file under ``strategies/module/data/``
(``tw_futures_chip.py``, ``tw_market_flow.py``, ...): base URL, optional
``FINMIND_TOKEN`` auth, and API-level error surfacing. What a dataset's
rows *mean* belongs in the caller, not here — this file only ever grows
by fixing/extending the raw call itself, never by adding a new dataset's
business logic.

Tracking table (verified live 2026-07-20; free tier / FINMIND_TOKEN, not
FinMind's paid Sponsor plan) — every dataset ever tried against this
client, regardless of which concept file consumes it. Update this list
whenever a new FinMind dataset is wired up anywhere — this is the one
place "what FinMind data do we have" should be answerable from.

wired (consumed by tw_futures_chip.py):
    - TaiwanFuturesInstitutionalInvestors
    - TaiwanOptionInstitutionalInvestors
    - TaiwanFuturesDealerTradingVolumeDaily

wired (consumed by tw_market_flow.py):
    - TaiwanStockTotalMarginPurchaseShortSale
    - TaiwanStockTotalInstitutionalInvestors

TODO — TaiwanFuturesDaily (open_interest field): needs front-month
rollover logic first. Data is per literal contract_date (e.g. "202607"),
not a TXFR1-style continuous alias — don't fake it with a naive
nearest-month pick, that silently misdates around rollover.

blocked — paid Sponsor tier, 400s even with a valid token (don't retry
speculatively; re-check this list if FinMind's tier policy ever changes):
    - TaiwanFuturesOpenInterestLargeTraders
    - TaiwanOptionOpenInterestLargeTraders
    - TaiwanOptionVix
    - TaiwanFuturesFinalSettlementPrice
    - TaiwanBusinessIndicator
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

_BASE_URL = "https://api.finmindtrade.com/api/v4/data"

# ---------------------------------------------------------------------------
# Shared vocabulary — FinMind's own taxonomy/ids, centralized here (not in
# each concept file) so a rename/addition happens in exactly one place.
# ---------------------------------------------------------------------------

# Canonical institution keys used by every FinMind dataset that breaks
# flow down by institution type. Each dataset spells them differently
# (see the two label dicts below) — this tuple is the one shared iteration
# order/membership check every concept file loops over.
INSTITUTION_KEYS = ("dealer", "trust", "foreign")

# institutional_investors label for *_InstitutionalInvestors datasets
# (TaiwanFuturesInstitutionalInvestors, TaiwanOptionInstitutionalInvestors)
# — consumed by tw_futures_chip.py.
DERIVATIVES_INSTITUTION_LABELS = {"dealer": "自營商", "trust": "投信", "foreign": "外資"}

# name label for TaiwanStockTotalInstitutionalInvestors (market-wide cash
# flow) — consumed by tw_market_flow.py. Includes "total", which only
# exists at this market-wide granularity (no per-institution-type analogue
# in the derivatives datasets above).
MARKET_INSTITUTION_LABELS = {
    "dealer": "Dealer_self", "trust": "Investment_Trust", "foreign": "Foreign_Investor", "total": "total",
}

# Project futures symbol (symbols.yaml) -> FinMind's own futures_id/option_id.
# This is FinMind's naming, not the project's — deliberately not folded into
# symbols.yaml (see architecture.md's "資料存取層設計" section).
FUTURES_ID_MAP = {"TXFR1": "TX", "MXFR1": "MTX", "TMFR1": "TMF"}
OPTION_ID_MAP = {"TXFR1": "TXO"}  # only TX has a listed options chain


def fetch(dataset: str, start_dt: datetime, end_dt: datetime, *, data_id: str | None = None) -> list[dict]:
    """Raw JSON rows for `dataset` over [start_dt, end_dt].

    `data_id` is omitted for market-wide datasets that don't take one
    (e.g. ``TaiwanStockTotalInstitutionalInvestors``).

    Raises:
        RuntimeError: Non-200 API response — includes datasets gated
            behind FinMind's paid Sponsor tier even with a valid token
            (confirmed 2026-07-20 for TaiwanFuturesOpenInterestLargeTraders,
            TaiwanOptionVix, TaiwanOptionOpenInterestLargeTraders,
            TaiwanFuturesFinalSettlementPrice, TaiwanBusinessIndicator —
            see this module's own docstring above for the full
            free-vs-paid tracking list).
    """
    params = {
        "dataset": dataset,
        "start_date": start_dt.date().isoformat(),
        "end_date": end_dt.date().isoformat(),
    }
    if data_id:
        params["data_id"] = data_id
    token = os.environ.get("FINMIND_TOKEN")
    if token:
        params["token"] = token

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())

    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind error for {dataset}/{data_id or '-'}: {payload.get('msg')!r}")
    return payload.get("data", [])
