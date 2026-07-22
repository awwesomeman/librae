"""US-listed single-stock social sentiment — ApeWisdom (Reddit mention/rank).

Snapshot-only (Path B, see ``factors.py``'s module docstring): ApeWisdom
returns a live ranking (current rank/mentions across the whole filtered
ticker list, refreshed ~twice hourly), not a queryable historical range for
one ticker, and gives no per-entry timestamp of its own — each row's
timestamp is collection time (when this ran).
"""
from __future__ import annotations

from strategies.module.data.factors import load_snapshot_factor, write_snapshot_factor
from strategies.module.data.providers import apewisdom

_FACTOR_MENTIONS = "us_social_mentions"
_FACTOR_RANK = "us_social_rank"
_SOURCE = "apewisdom"
_DEFAULT_FILTER = "all-stocks"


def collect_social_mentions(symbol: str, filter_: str = _DEFAULT_FILTER) -> int:
    """Fetch the current ApeWisdom snapshot for `filter_` and, if `symbol`
    appears in it, write one mentions row + one rank row. Returns rows
    written (0, 1, or 2)."""
    rows = apewisdom.fetch(filter_)
    match = next((r for r in rows if r.get("ticker") == symbol), None)
    if match is None:
        return 0

    written = write_snapshot_factor(symbol, _FACTOR_MENTIONS, _SOURCE, match["mentions"], frequency="H1")
    written += write_snapshot_factor(symbol, _FACTOR_RANK, _SOURCE, match["rank"], frequency="H1")
    return written


def load_social_mentions(symbol: str, start: str | None = None, end: str | None = None):
    return load_snapshot_factor(symbol, _FACTOR_MENTIONS, _SOURCE, start=start, end=end)


def load_social_rank(symbol: str, start: str | None = None, end: str | None = None):
    return load_snapshot_factor(symbol, _FACTOR_RANK, _SOURCE, start=start, end=end)
