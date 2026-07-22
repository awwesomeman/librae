"""US-listed single-stock analyst sentiment — Finnhub free tier.

Snapshot-only (Path B, see ``factors.py``'s module docstring): Finnhub's
free tier silently ignores `from`/`to` params on these endpoints — verified
live 2026-07-22, always the same trailing ~4 periods regardless of range
asked. Unlike ApeWisdom though, each row *does* carry a real "as of" date
(recommendation period / earnings report date), so that's used as `ts`
instead of collection time — re-running just re-upserts the same rows
(external_factors is keyed through timestamp, so this is idempotent).

    us_analyst_recommendation_score — 1 (strongSell) to 5 (strongBuy),
                                       analyst count weighted average
    us_earnings_surprise_pct        — (actual - estimate) / |estimate| * 100,
                                       ts = report date, not period end
                                       (surprise isn't public until reported)
"""
from __future__ import annotations

import pandas as pd

from strategies.module.data.factors import load_snapshot_factor, write_snapshot_factor
from strategies.module.data.providers import finnhub as finnhub_client

_FACTOR_RECOMMENDATION = "us_analyst_recommendation_score"
_FACTOR_EARNINGS_SURPRISE = "us_earnings_surprise_pct"
_SOURCE = "finnhub"
_RECOMMENDATION_WEIGHTS = {"strongBuy": 5, "buy": 4, "hold": 3, "sell": 2, "strongSell": 1}


def collect_analyst_recommendation(symbol: str) -> int:
    """Fetch Finnhub's trailing recommendation-trend snapshot for `symbol`
    and write one weighted score per period returned. Returns rows written."""
    rows = finnhub_client.fetch("stock/recommendation", {"symbol": symbol})
    written = 0
    for r in rows:
        total = sum(r[k] for k in _RECOMMENDATION_WEIGHTS)
        if total == 0:
            continue
        score = sum(r[k] * w for k, w in _RECOMMENDATION_WEIGHTS.items()) / total
        written += write_snapshot_factor(
            symbol, _FACTOR_RECOMMENDATION, _SOURCE, score,
            frequency="MN1", ts=pd.Timestamp(r["period"], tz="UTC"),
        )
    return written


def collect_earnings_surprise(symbol: str) -> int:
    """Fetch Finnhub's trailing earnings-calendar snapshot for `symbol` and
    write one surprise-% row per already-reported period. Returns rows
    written (skips scheduled-but-not-yet-reported entries)."""
    payload = finnhub_client.fetch("calendar/earnings", {"symbol": symbol})
    written = 0
    for r in payload.get("earningsCalendar", []):
        actual, estimate = r.get("epsActual"), r.get("epsEstimate")
        if actual is None or not estimate:
            continue
        surprise_pct = (actual - estimate) / abs(estimate) * 100
        written += write_snapshot_factor(
            symbol, _FACTOR_EARNINGS_SURPRISE, _SOURCE, surprise_pct,
            frequency="IRREGULAR", ts=pd.Timestamp(r["date"], tz="UTC"),
        )
    return written


def load_analyst_recommendation(symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    return load_snapshot_factor(symbol, _FACTOR_RECOMMENDATION, _SOURCE, start=start, end=end)


def load_earnings_surprise(symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    return load_snapshot_factor(symbol, _FACTOR_EARNINGS_SURPRISE, _SOURCE, start=start, end=end)
