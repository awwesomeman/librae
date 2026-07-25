"""Thin SEC EDGAR REST client — company facts (XBRL), no business logic.

SEC EDGAR requires a descriptive User-Agent on every request (fair-access
policy — generic/unlabeled clients get rate-limited or blocked). Reads
``SEC_EDGAR_USER_AGENT`` from env (format: ``"<app/contact> <email>"``);
falls back to a placeholder that still identifies this project rather than
spoofing a browser UA — set the env var for anything beyond light local use.

No API key. Documented fair-access limit: 10 requests/second — see
https://www.sec.gov/os/webmaster-faq#developers. No per-day cap, unlike
FinMind/FMP-style commercial tiers.

Ticker -> CIK mapping: SEC's own bulk lookup file
(``https://www.sec.gov/files/company_tickers.json``) would need a network
call just to resolve symbols this repo already knows, so tickers actually
used here are hardcoded below instead. Extend as new US tickers are added.
"""
from __future__ import annotations

import json
import os
import urllib.request

_USER_AGENT = os.environ.get("SEC_EDGAR_USER_AGENT", "librae research-contact@example.com")

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


def fetch_submissions(ticker: str) -> dict:
    """Raw filing-history JSON for `ticker` — same submissions endpoint SEC's
    own EDGAR full-text search uses. Unlike companyfacts (XBRL financial
    concepts only), ``filings.recent`` here lists *every* filing type
    against the issuer's own CIK, including Form 3/4/5 insider ownership
    filings — no separate insider-specific endpoint needed.

    Raises:
        ValueError: If `ticker` has no entry in ``TICKER_TO_CIK``.
    """
    cik = TICKER_TO_CIK.get(ticker.upper())
    if cik is None:
        raise ValueError(f"No CIK mapping for ticker={ticker!r}. Add it to TICKER_TO_CIK.")

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_filing_document(cik: str, accession_number: str, document: str) -> bytes:
    """Raw bytes of one document within a filing (e.g. a Form 4's
    ``primarydocument.xml``) — `cik` unpadded is fine, `accession_number`
    with or without dashes, both normalized here since submissions JSON and
    EDGAR's own Archives paths spell them differently."""
    cik_num = str(int(cik))
    accession_nodash = accession_number.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{accession_nodash}/{document}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()
