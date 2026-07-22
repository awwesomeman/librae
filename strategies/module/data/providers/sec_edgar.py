"""Thin SEC EDGAR REST client — company facts (XBRL), no business logic.

SEC EDGAR requires a descriptive User-Agent on every request (fair-access
policy — generic/unlabeled clients get rate-limited or blocked). Reads
``SEC_EDGAR_USER_AGENT`` from env (format: ``"<app/contact> <email>"``);
falls back to a placeholder that still identifies this project rather than
spoofing a browser UA — set the env var for anything beyond light local use.

Ticker -> CIK mapping: SEC's own bulk lookup file
(``https://www.sec.gov/files/company_tickers.json``) would need a network
call just to resolve symbols this repo already knows, so tickers actually
used here are hardcoded below instead. Extend as new US tickers are added.
"""
from __future__ import annotations

import json
import os
import urllib.request

_USER_AGENT = os.environ.get("SEC_EDGAR_USER_AGENT", "quant-strategy-lab research-contact@example.com")

# ticker -> 10-digit zero-padded CIK
TICKER_TO_CIK = {
    "MU": "0000723125",
}


def fetch_company_facts(ticker: str) -> dict:
    """Raw XBRL company facts JSON for `ticker` (e.g. ``"MU"``).

    Single payload covering the company's full filing history — SEC EDGAR
    has no date-range query parameter, callers slice by date themselves.

    Raises:
        ValueError: If `ticker` has no entry in ``TICKER_TO_CIK``.
    """
    cik = TICKER_TO_CIK.get(ticker.upper())
    if cik is None:
        raise ValueError(f"No CIK mapping for ticker={ticker!r}. Add it to TICKER_TO_CIK.")

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())
