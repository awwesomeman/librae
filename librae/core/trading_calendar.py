"""Trading-session labels and session-aligned OHLCV resampling.

Bar timestamps remain UTC-aware bar-start instants.  A calendar maps those
instants to per-instrument session labels; a timezone or ``timestamp.date()``
cannot do that for markets whose trading day begins on a prior calendar day.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from functools import cache
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from exchange_calendars import ExchangeCalendar

ALWAYS_OPEN_CALENDAR = "24/7"
TAIFEX_INDEX_CALENDAR = "XTAIFEX"
TAIFEX_LATE_OPEN_CALENDAR = "XTAIFEX_1725"
_TAIFEX_CALENDARS = frozenset({TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR})
_TAIPEI = "Asia/Taipei"


def _timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tz is None:
        raise ValueError("bar timestamps must be timezone-aware")
    return timestamp.tz_convert("UTC")


@cache
def _exchange_calendar(calendar_id: str) -> ExchangeCalendar:
    try:
        import exchange_calendars as xcals
    except ImportError as exc:  # pragma: no cover - package dependency contract
        raise RuntimeError(
            "trading calendars require exchange-calendars; install the librae package "
            "with its declared dependencies"
        ) from exc

    exchange_id = "XTAI" if calendar_id in _TAIFEX_CALENDARS else calendar_id
    try:
        return xcals.get_calendar(exchange_id)
    except InvalidCalendarName as exc:
        raise ValueError(f"unknown calendar_id={calendar_id!r}") from exc


try:
    from exchange_calendars.errors import InvalidCalendarName
except ImportError:  # pragma: no cover - resolved by _exchange_calendar's friendly error
    InvalidCalendarName = ValueError


def _calendar_session(calendar: ExchangeCalendar, value: date) -> pd.Timestamp:
    label = pd.Timestamp(value)
    if not calendar.is_session(label):
        raise ValueError(f"{value.isoformat()} is not a trading session for {calendar.name}")
    return label


def _taifex_session_label(timestamp: pd.Timestamp, calendar_id: str) -> date:
    local = timestamp.tz_convert(_TAIPEI)
    local_date = local.date()
    local_time = local.time()
    night_open = time(15, 0) if calendar_id == TAIFEX_INDEX_CALENDAR else time(17, 25)
    xtai = _exchange_calendar(calendar_id)

    if night_open <= local_time:
        prior_session = _calendar_session(xtai, local_date)
        return xtai.next_session(prior_session).date()
    if local_time < time(5, 0):
        prior_date = local_date - timedelta(days=1)
        prior_session = _calendar_session(xtai, prior_date)
        return xtai.next_session(prior_session).date()
    if time(8, 45) <= local_time < time(13, 45):
        return _calendar_session(xtai, local_date).date()
    raise ValueError(f"{timestamp.isoformat()} is outside the {calendar_id} trading sessions")


def session_label(value: object, calendar_id: str) -> date:
    """Return the exchange trading-date label for a UTC-aware bar start."""
    timestamp = _timestamp(value)
    if calendar_id == ALWAYS_OPEN_CALENDAR:
        return timestamp.date()
    if calendar_id in _TAIFEX_CALENDARS:
        return _taifex_session_label(timestamp, calendar_id)

    calendar = _exchange_calendar(calendar_id)
    minute = timestamp.floor("min")
    try:
        return calendar.minute_to_session(minute, direction="none").date()
    except ValueError as exc:
        raise ValueError(
            f"{timestamp.isoformat()} is outside the {calendar_id} trading session"
        ) from exc


def validate_calendar_id(calendar_id: str) -> None:
    """Resolve a calendar identifier without requiring an in-session timestamp."""
    if calendar_id != ALWAYS_OPEN_CALENDAR:
        _exchange_calendar(calendar_id)


def session_labels(index: pd.DatetimeIndex, calendar_id: str) -> pd.Index:
    """Vector-shaped session labels for a timezone-aware bar-start index."""
    if index.tz is None:
        raise ValueError("bar timestamps must be timezone-aware")
    return pd.Index(
        [session_label(timestamp, calendar_id) for timestamp in index],
        name="session_label",
    )


def _local_timestamp(day: date, clock: time, timezone: str) -> pd.Timestamp:
    return pd.Timestamp.combine(day, clock).tz_localize(timezone).tz_convert("UTC")


def _session_segments(
    calendar_id: str, label: date
) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
    if calendar_id == ALWAYS_OPEN_CALENDAR:
        start = pd.Timestamp(label, tz="UTC")
        return ((start, start + pd.Timedelta(days=1)),)

    calendar = _exchange_calendar(calendar_id)
    session = _calendar_session(calendar, label)
    if calendar_id in _TAIFEX_CALENDARS:
        previous_session = calendar.previous_session(session).date()
        night_open = time(15, 0) if calendar_id == TAIFEX_INDEX_CALENDAR else time(17, 25)
        return (
            (
                _local_timestamp(previous_session, night_open, _TAIPEI),
                _local_timestamp(label, time(5, 0), _TAIPEI),
            ),
            (
                _local_timestamp(label, time(8, 45), _TAIPEI),
                _local_timestamp(label, time(13, 45), _TAIPEI),
            ),
        )

    schedule = calendar.schedule.loc[session]
    open_at = pd.Timestamp(schedule["open"])
    close_at = pd.Timestamp(schedule["close"])
    break_start = schedule.get("break_start")
    break_end = schedule.get("break_end")
    if pd.notna(break_start) and pd.notna(break_end):
        return (
            (open_at, pd.Timestamp(break_start)),
            (pd.Timestamp(break_end), close_at),
        )
    return ((open_at, close_at),)


def _bucket_start(value: object, target_seconds: int, calendar_id: str) -> pd.Timestamp:
    timestamp = _timestamp(value)
    label = session_label(timestamp, calendar_id)
    segments = _session_segments(calendar_id, label)
    if target_seconds >= 24 * 60 * 60:
        return segments[0][0]
    for segment_open, segment_close in segments:
        if segment_open <= timestamp < segment_close:
            offset_seconds = int((timestamp - segment_open).total_seconds())
            return segment_open + pd.Timedelta(
                seconds=offset_seconds - offset_seconds % target_seconds
            )
    raise ValueError(f"{timestamp.isoformat()} is outside the {calendar_id} trading segments")


def bar_close(value: object, target_seconds: int, calendar_id: str) -> pd.Timestamp:
    """Return the actual close of a session-aligned bar-start timestamp."""
    if target_seconds <= 0:
        raise ValueError("target_seconds must be positive")
    timestamp = _timestamp(value)
    segments = _session_segments(calendar_id, session_label(timestamp, calendar_id))
    if target_seconds >= 24 * 60 * 60:
        return segments[-1][1]
    for segment_open, segment_close in segments:
        if segment_open <= timestamp < segment_close:
            return min(timestamp + pd.Timedelta(seconds=target_seconds), segment_close)
    raise ValueError(f"{timestamp.isoformat()} is outside the {calendar_id} trading segments")


def resample_session_ohlcv(
    frame: pd.DataFrame,
    target_seconds: int,
    calendar_id: str,
) -> pd.DataFrame:
    """Aggregate 1-minute OHLCV on exchange-session bucket boundaries."""
    if target_seconds <= 0:
        raise ValueError("target_seconds must be positive")
    if target_seconds > 24 * 60 * 60:
        raise ValueError("session-aligned resampling supports intervals up to D1")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("OHLCV must have a timezone-aware DatetimeIndex")

    buckets = pd.DatetimeIndex(
        [_bucket_start(timestamp, target_seconds, calendar_id) for timestamp in frame.index]
    )
    grouped = frame.groupby(buckets)
    result = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
        }
    )
    result.index.name = frame.index.name
    return result
