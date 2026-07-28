"""Shioaji-specific timestamp normalization."""

from __future__ import annotations

TAIPEI_UTC_OFFSET_SEC = 8 * 60 * 60


def shioaji_ts_ns_to_epoch(ts_ns: int) -> int:
    """Convert Shioaji's fake-UTC Taipei wall clock to a true UTC epoch."""
    return int(ts_ns) // 1_000_000_000 - TAIPEI_UTC_OFFSET_SEC
