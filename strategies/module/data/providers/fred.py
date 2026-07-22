"""Thin FRED (St. Louis Fed) REST client — one function, no business logic.

Reads ``FRED_API_KEY`` from env (free, same-day approval at
https://fredaccount.stlouisfed.org/apikeys). FRED has no rate-limit
surprises and no paid tier gating — unlike FinMind, no free/paid split to
track here.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime

_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


def fetch(series_id: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Raw observations for `series_id` over [start_dt, end_dt].

    Each row: {"date": "YYYY-MM-DD", "value": str} — FRED returns "." for
    missing observations (holidays/no-release days), caller's job to filter.

    Raises:
        RuntimeError: Missing FRED_API_KEY or non-200 API response.
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("FRED_API_KEY not set — get a free key at https://fredaccount.stlouisfed.org/apikeys")

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_dt.date().isoformat(),
        "observation_end": end_dt.date().isoformat(),
    }
    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"FRED error for {series_id}: HTTP {e.code} {e.reason}") from e

    return payload.get("observations", [])
