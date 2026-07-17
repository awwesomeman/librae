"""Asia/Taipei calendar helpers for Shioaji kbar data.

Shioaji's kbars() ``ts`` field encodes Taipei wall-clock time as if it were a
UTC epoch (confirmed against a live snapshot: reading the raw value as UTC
read exactly 8h ahead of actual UTC). Every function below operates on the
*true* UTC epoch (seconds) — apply shioaji_ts_ns_to_epoch() first.

TAIFEX session windows (Taipei local time, https://www.taifex.com.tw/cht/4/aHIntroduction):
day session 08:45-13:45, night session 15:00 to next day 05:00. Sub-daily
bars must restart their bucketing at these session opens (not a round-hour
grid), and daily bars must attribute the night session to the day it closes
into, not the day it opened on.
"""
from __future__ import annotations

import pandas as pd

TAIPEI_UTC_OFFSET_SEC = 8 * 3600
_DAY_SEC = 86400

DAY_SESSION_OPEN_SEC = 8 * 3600 + 45 * 60     # 08:45
DAY_SESSION_CLOSE_SEC = 13 * 3600 + 45 * 60   # 13:45
NIGHT_SESSION_OPEN_SEC = 15 * 3600            # 15:00
NIGHT_SESSION_CLOSE_SEC = 5 * 3600            # 05:00 (next calendar day)


def shioaji_ts_ns_to_epoch(ts_ns: int) -> int:
    """Convert Shioaji kbars() raw ``ts`` (nanoseconds) to a true UTC epoch (seconds)."""
    return int(ts_ns) // 1_000_000_000 - TAIPEI_UTC_OFFSET_SEC


def _taipei_seconds_of_day(ts: int) -> int:
    """Seconds since Taipei-local midnight for a true UTC epoch ``ts``."""
    return (ts + TAIPEI_UTC_OFFSET_SEC) % _DAY_SEC


def floor_to_session_start(ts: int, target_seconds: int) -> int:
    """Floor ``ts`` (true UTC epoch) to a ``target_seconds``-wide bucket
    anchored to the open of whichever TAIFEX session ``ts`` falls in.

    A plain epoch grid puts sub-daily boundaries at round Taipei clock hours
    (09:00, 10:00, ...), not the market's actual session opens — the first
    bar of a session would land in a bucket labeled before trading even
    started. Timestamps outside both sessions (the 05:00-08:45 or
    13:45-15:00 gaps) shouldn't occur in real data; they fall back to that
    day's day-session open so this still returns a deterministic bucket.
    """
    time_of_day = _taipei_seconds_of_day(ts)
    midnight = ts - time_of_day
    if DAY_SESSION_OPEN_SEC <= time_of_day < DAY_SESSION_CLOSE_SEC:
        session_open = midnight + DAY_SESSION_OPEN_SEC
    elif time_of_day >= NIGHT_SESSION_OPEN_SEC:
        session_open = midnight + NIGHT_SESSION_OPEN_SEC
    elif time_of_day < NIGHT_SESSION_CLOSE_SEC:
        session_open = midnight - _DAY_SEC + NIGHT_SESSION_OPEN_SEC
    else:
        session_open = midnight + DAY_SESSION_OPEN_SEC
    offset = ts - session_open
    return session_open + offset - (offset % target_seconds)


def floor_to_trading_day(ts: int) -> int:
    """Floor ``ts`` (true UTC epoch) to the TAIFEX trading day it belongs to.

    The night session (15:00 Taipei -> 05:00 next day) is attributed to the
    day it closes into, not the calendar day it opened on (TAIFEX/IBKR
    convention). Day-session bars use their own calendar day as usual.
    """
    time_of_day = _taipei_seconds_of_day(ts)
    midnight = ts - time_of_day
    return midnight + _DAY_SEC if time_of_day >= NIGHT_SESSION_OPEN_SEC else midnight


def resample_taifex_ohlcv(df: pd.DataFrame, target_seconds: int) -> pd.DataFrame:
    """Resample a Shioaji 1-min OHLCV DataFrame to ``target_seconds`` bars,
    bucketed on TAIFEX session/trading-day boundaries instead of a plain
    UTC-epoch grid.

    Args:
        df: DataFrame with a true-UTC DatetimeIndex (after
            shioaji_ts_ns_to_epoch correction) and open/high/low/close/volume
            columns.
        target_seconds: Bar width in seconds (e.g. 3600 for 1h, 86400 for 1d).
    """
    # WHY: DatetimeIndex internal resolution (ns/us/s) varies by pandas
    # version/construction path — asi8 // 1e9 would silently assume ns and
    # misparse other resolutions. Subtracting the epoch as a Timestamp and
    # floor-dividing by a 1s Timedelta is resolution-agnostic.
    epoch = (df.index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)
    if target_seconds >= _DAY_SEC:
        bucket_epoch = [floor_to_trading_day(ts) for ts in epoch]
    else:
        bucket_epoch = [floor_to_session_start(ts, target_seconds) for ts in epoch]
    bucket = pd.to_datetime(bucket_epoch, unit="s", utc=True)

    grouped = df.groupby(bucket)
    out = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "volume": grouped["volume"].sum(),
    })
    out.index.name = df.index.name
    return out
