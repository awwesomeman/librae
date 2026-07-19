"""Shared data utilities — datetime parsing, OHLCV resampling.

Generic helpers used across data sources and strategies.
No exchange-specific logic belongs here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


def parse_dt(value: str | datetime) -> datetime:
    """Parse a datetime value to UTC-aware datetime.

    Handles: naive datetime (assumed UTC), tz-aware datetime (converted),
    ISO-format strings (with or without timezone).
    """
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def subtract_months(dt: datetime, months: int) -> datetime:
    """Subtract *months* from a datetime, clamping day to 28."""
    month = dt.month - months
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(dt.day, 28)
    return dt.replace(year=year, month=month, day=day)


def compute_coverage_gaps(
    ranges: list[tuple[datetime, datetime]],
    start_dt: datetime,
    end_dt: datetime,
) -> list[tuple[datetime, datetime]]:
    """Sub-ranges of [start_dt, end_dt] not covered by any of ``ranges`` (sorted).

    Shared by every DB-first + coverage-tracked cache in strategies/data/
    (``ohlcv.py``'s ``get_ohlcv``, ``factors.py``'s ``get_factor``) — the gap
    math is identical regardless of what's being cached, only the DB
    tables/keys differ.

    Returns list of (gap_start, gap_end) tuples. Empty list = no gaps.
    """
    gaps: list[tuple[datetime, datetime]] = []
    cursor = start_dt
    for range_started_at, range_ended_at in ranges:
        if range_ended_at < cursor:
            continue
        if range_started_at > end_dt:
            break
        if range_started_at > cursor:
            gaps.append((cursor, min(range_started_at, end_dt)))
        cursor = max(cursor, range_ended_at)
        if cursor >= end_dt:
            break
    if cursor < end_dt:
        gaps.append((cursor, end_dt))
    return gaps


def resample_ohlcv(df: pd.DataFrame, rule: str = "1D") -> pd.DataFrame:
    """Resample OHLCV DataFrame to a different timeframe.

    Args:
        df: DataFrame with DatetimeIndex and open/high/low/close/volume columns.
        rule: Pandas resample rule (e.g. ``"1D"``, ``"4h"``, ``"30min"``).
    """
    x = pd.DataFrame()
    x["open"] = df["open"].resample(rule).first()
    x["high"] = df["high"].resample(rule).max()
    x["low"] = df["low"].resample(rule).min()
    x["close"] = df["close"].resample(rule).last()
    x["volume"] = df["volume"].resample(rule).sum()
    return x.dropna()
