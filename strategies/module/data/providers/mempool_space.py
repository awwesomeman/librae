"""Thin mempool.space client — Bitcoin network hashrate/difficulty/mempool
congestion, no business logic. No API key, no documented rate limit for
this usage (low-frequency public GETs, verified live 2026-07-22).
"""
from __future__ import annotations

import json
import urllib.request

import pandas as pd

_BASE_URL = "https://mempool.space/api/v1"


def _get(path: str) -> object:
    req = urllib.request.Request(f"{_BASE_URL}/{path}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_hashrate() -> pd.DataFrame:
    """Full daily network-hashrate history (from genesis) — columns
    ``[date, value]``, `value` in hashes/sec."""
    raw = _get("mining/hashrate/all")["hashrates"]
    records = [{"date": pd.Timestamp(r["timestamp"], unit="s", tz="UTC"), "value": r["avgHashrate"]} for r in raw]
    return pd.DataFrame(records).sort_values("date").reset_index(drop=True)


def fetch_difficulty_adjustments() -> pd.DataFrame:
    """Full difficulty-retarget history (from genesis, one row per ~2-week
    retarget event) — columns ``[date, value]``, `value` = difficulty level.

    Each row is ``[timestamp, block_height, difficulty, adjustment_pct]``;
    only timestamp/difficulty are kept here.
    """
    raw = _get("mining/difficulty-adjustments/all")
    records = [{"date": pd.Timestamp(r[0], unit="s", tz="UTC"), "value": r[2]} for r in raw]
    return pd.DataFrame(records).sort_values("date").reset_index(drop=True)


def fetch_mempool_stats() -> pd.DataFrame:
    """Full mempool-congestion history (from 2016-12, ~2 readings/day) —
    columns ``[date, value]``, `value` = pending tx count in the mempool.
    """
    raw = _get("statistics/all")
    records = [{"date": pd.Timestamp(r["added"], unit="s", tz="UTC"), "value": r["count"]} for r in raw]
    return pd.DataFrame(records).sort_values("date").reset_index(drop=True)
