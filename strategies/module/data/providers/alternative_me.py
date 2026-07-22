"""Thin alternative.me client — Fear & Greed Index, no business logic.

Used by ``strategies/module/data/regime.py`` (fng_regime). ``httpx`` is a core
dependency (no extra needed), unlike the yfinance client next to this file.

No API key, no documented rate limit (small hobby API — be conservative,
this repo only ever calls it once per regime computation, not per-bar).
"""
from __future__ import annotations

import httpx

_URL = "https://api.alternative.me/fng/"


def fetch_fng(limit: int = 1000) -> list[dict]:
    """Raw Fear & Greed Index records (most recent `limit` days), each
    ``{"value": str, "value_classification": str, "timestamp": str, ...}``.

    Returns ``[]`` on any request failure — external API availability
    shouldn't crash the research pipeline; the caller decides the
    neutral-default fallback.
    """
    try:
        r = httpx.get(_URL, params={"limit": limit, "format": "json"}, timeout=15)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception:
        return []
