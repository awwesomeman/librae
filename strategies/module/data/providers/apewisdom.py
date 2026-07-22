"""Thin ApeWisdom REST client — one function, no business logic.

No API key, no documented rate limit (verified live 2026-07-22). Unlike
FinMind/FRED there's no auth to thread through — every call is anonymous.
"""
from __future__ import annotations

import json
import urllib.request

_BASE_URL = "https://apewisdom.io/api/v1.0/filter"


def fetch(filter_: str, page: int = 1) -> list[dict]:
    """One page of ranked mention rows for `filter_` (e.g. ``"all-stocks"``,
    ``"wallstreetbets"``) — each row: rank, ticker, name, mentions,
    mentions_24h_ago, rank_24h_ago, upvotes. Current snapshot only, no
    historical range — same limitation as yfinance short interest (see
    ``us_chip.py``).

    Returns ``[]`` on any request failure.
    """
    url = f"{_BASE_URL}/{filter_}/page/{page}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except Exception:
        return []
    return payload.get("results", [])
