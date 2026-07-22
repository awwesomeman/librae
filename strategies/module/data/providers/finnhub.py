"""Thin Finnhub REST client — one function, no business logic.

Reads ``FINNHUB_API_KEY`` from env (free registration at
https://finnhub.io/register). Free-tier verified live 2026-07-22:
``stock/recommendation`` (analyst buy/hold/sell trend) and ``stock/earnings``
(EPS actual vs estimate) both work; ``stock/congressional-trading``,
``institutional/ownership``, ``stock/social-sentiment``, ``stock/price-target``
all 403 on the free tier — don't wire those up without re-verifying a paid
key changes that. ``insider-transactions`` also works free but isn't worth
wiring: it re-serves SEC's own Form 4 data (``source: "sec"`` in the
response) that ``us_insider.py`` already reads directly from EDGAR, more
correctly (no third-party relay lag/parsing).

Free tier also caps historical depth to a fixed trailing window (~4 periods)
regardless of ``from``/``to`` params — verified live, those params are
silently ignored. See ``us_analyst.py`` for why that forces the
write_snapshot_factor() path, not register_factor_fetcher().
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

_BASE_URL = "https://finnhub.io/api/v1"


def fetch(endpoint: str, params: dict) -> dict | list:
    """Raw JSON for `endpoint` (e.g. ``"stock/recommendation"``) with
    `params` plus the API key — Finnhub's own query-string auth.

    Raises:
        RuntimeError: Missing FINNHUB_API_KEY, or a non-200 response
            (including the free tier's 403 on gated endpoints).
    """
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY not set — get a free key at https://finnhub.io/register")

    query = {**params, "token": api_key}
    url = f"{_BASE_URL}/{endpoint}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Finnhub error for {endpoint}: HTTP {e.code} {e.reason}") from e
