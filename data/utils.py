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
