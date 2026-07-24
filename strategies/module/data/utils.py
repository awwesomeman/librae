"""Shared data utilities — datetime parsing, OHLCV resampling.

Generic helpers used across data sources and strategies.
No exchange-specific logic belongs here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

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

    Shared by every DB-first + coverage-tracked cache in strategies/module/data/
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


def merge_asof_backward(base: pd.DataFrame, other: pd.DataFrame, on: str = "timestamp") -> pd.DataFrame:
    """Backward asof-merge `other` onto `base` on a shared UTC timestamp column.

    Shared by every ``attach_*_features`` in strategies/module/data/ (funding,
    open_interest, cross_asset, ...) — each base row only sees `other` rows
    at or before its own timestamp, so joining a lower-frequency or
    differently-timed external series never leaks a future value into a
    bar that predates it. Both frames are copied and re-sorted; neither
    input is mutated.
    """
    base = base.copy()
    other = other.copy()
    base[on] = pd.to_datetime(base[on], utc=True).astype("datetime64[ns, UTC]")
    other[on] = pd.to_datetime(other[on], utc=True).astype("datetime64[ns, UTC]")
    base = base.sort_values(on)
    other = other.sort_values(on)
    return pd.merge_asof(base, other, on=on, direction="backward")


def merge_native_transform_backward(
    ohlcv: pd.DataFrame, native: pd.DataFrame, transform: Callable[[pd.DataFrame], pd.DataFrame], on: str = "timestamp",
) -> pd.DataFrame:
    """Run `transform` on `native` (its own pre-merge frequency) before
    asof-merging onto `ohlcv`. Structural fix for the funding_z_3d
    window-semantics bug (RESEARCH_METHODOLOGY.md's known pitfalls): a
    rolling/z-score/pct_change computed *after* merging a coarser-frequency
    source onto `ohlcv`'s own base_tf silently means "N bars of base_tf",
    not "N native periods". Routing through here makes that mistake
    structurally hard — `transform` only ever sees `native` at its own
    frequency. `transform` adds columns to `native` sorted by `on`; keep the
    raw value column too if it should also be merged.
    """
    native = transform(native.sort_values(on).copy())
    return merge_asof_backward(ohlcv, native, on=on)


def taipei_date_to_utc(date_str: str) -> pd.Timestamp:
    """Convert a Taipei calendar-day string (``"YYYY-MM-DD"``) to its UTC
    midnight timestamp. Shared by every Taiwan daily-frequency factor
    (tw_futures_chip.py, tw_market_flow.py, ...) that gets a bare date
    string back from its data source, not an intraday timestamp."""
    return pd.Timestamp(date_str, tz="Asia/Taipei").tz_convert("UTC")


def attach_or_zero_fill(df: pd.DataFrame, col: str, series: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """Merge `series[value_col]` onto `df` as `col` via ``merge_asof_backward``,
    or fill `col` with 0.0 if `series` is empty ("no data available yet",
    not a real zero reading — same convention every attach_*_features uses).

    Shared by any attach_*_features that merges several optional external
    factors in a loop (tw_futures_chip.py, tw_market_flow.py, ...) — pulls
    the merge-or-zero-fill boilerplate out of each loop body.
    """
    if series.empty:
        df = df.copy()
        df[col] = 0.0
        return df
    df = merge_asof_backward(df, series.rename(columns={value_col: col}))
    df[col] = df[col].fillna(0.0)
    return df


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
