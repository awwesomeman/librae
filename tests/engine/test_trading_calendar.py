from __future__ import annotations

from datetime import date, timedelta
from functools import cache
from pathlib import Path

import pandas as pd
import pytest
from librae.core.trading_calendar import (
    _CANCELLED_NIGHTS,
    _SESSION_HOURS,
    _XTAI_OVERRIDES,
    TAIFEX_INDEX_CALENDAR,
    TAIFEX_LATE_OPEN_CALENDAR,
    _exchange_calendar,
    _hours,
    _session_segments,
    _session_table,
    bar_close,
    bar_start_problem,
    execution_session_label,
    next_segment_open_after,
    period_close,
    period_start,
    resample_session_ohlcv,
    session_bounds,
    session_label,
    session_lookback_days,
    validate_session_mode,
)


def _taipei(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="Asia/Taipei").tz_convert("UTC")


def test_taifex_night_session_uses_next_exchange_session() -> None:
    assert session_label(
        _taipei("2026-04-10 15:30"),
        TAIFEX_INDEX_CALENDAR,
    ) == date(2026, 4, 13)
    assert session_label(
        _taipei("2026-04-11 02:30"),
        TAIFEX_INDEX_CALENDAR,
    ) == date(2026, 4, 13)


def test_taifex_day_session_uses_same_session_label() -> None:
    assert session_label(
        _taipei("2026-04-13 09:30"),
        TAIFEX_INDEX_CALENDAR,
    ) == date(2026, 4, 13)


def test_taifex_rejects_out_of_session_timestamp() -> None:
    with pytest.raises(ValueError, match="outside"):
        session_label(
            _taipei("2026-04-13 07:00"),
            TAIFEX_INDEX_CALENDAR,
        )


def test_taifex_rejects_weekend_afternoon_between_night_close_and_next_open() -> None:
    # Friday's night session physically closes Saturday 05:00; Saturday
    # afternoon is a dead period even though the next regular session
    # (Monday) is still the session label for Friday's night bars.
    with pytest.raises(ValueError, match="outside"):
        session_label(
            _taipei("2026-04-11 14:00"),
            TAIFEX_INDEX_CALENDAR,
        )


def test_late_open_taifex_product_rejects_early_night_timestamp() -> None:
    with pytest.raises(ValueError, match="outside"):
        session_label(
            _taipei("2026-04-13 16:00"),
            TAIFEX_LATE_OPEN_CALENDAR,
        )


def test_xtai_stock_session_is_not_taifex_session() -> None:
    stock_bar = _taipei("2026-04-01 09:00")
    assert session_label(stock_bar, "XTAI") == date(2026, 4, 1)

    with pytest.raises(ValueError, match="outside"):
        session_label(_taipei("2026-04-01 08:45"), "XTAI")


def test_unknown_calendar_fails_explicitly() -> None:
    with pytest.raises(ValueError, match="unknown calendar_id"):
        session_label(pd.Timestamp("2026-04-01 00:00Z"), "NOT_A_CALENDAR")


@pytest.mark.parametrize(
    ("calendar_id", "instant"),
    [
        ("XNYS", "2000-01-05 14:30Z"),
        ("XTAI", "2000-01-05 02:00Z"),
        # A TAIFEX session's night segment opens on the session before it.
        (TAIFEX_INDEX_CALENDAR, "2000-01-05 02:00Z"),
    ],
)
def test_calendars_reach_back_past_the_library_default(calendar_id: str, instant: str) -> None:
    """exchange_calendars builds only the last 20 years unless given a start."""
    assert session_label(pd.Timestamp(instant), calendar_id) == date(2000, 1, 5)


def test_a_calendar_that_cannot_reach_back_that_far_starts_at_its_own_bound() -> None:
    calendar = _exchange_calendar("XSAU")  # holidays recorded from 2021 only
    assert calendar.first_session == calendar.sessions[calendar.sessions >= calendar.bound_min()][0]


def test_xtai_and_the_taifex_calendars_share_one_calendar() -> None:
    xtai = _exchange_calendar("XTAI")
    assert _exchange_calendar(TAIFEX_INDEX_CALENDAR) is xtai
    assert _exchange_calendar(TAIFEX_LATE_OPEN_CALENDAR) is xtai


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        session_label(pd.Timestamp("2026-04-01 00:00"), "24/7")


def test_session_lookback_days_includes_weekends_and_holidays() -> None:
    assert session_lookback_days(pd.Timestamp("2025-01-04T00:00:00Z"), 5, "XNYS") == 8


def test_session_lookback_days_for_always_open_market_matches_periods() -> None:
    assert session_lookback_days(pd.Timestamp("2025-01-04T00:00:00Z"), 5, "24/7") == 5


def test_resample_hourly_taifex_bars_anchor_to_each_session_open() -> None:
    index = pd.DatetimeIndex(
        [
            _taipei("2026-04-01 08:45"),
            _taipei("2026-04-01 09:00"),
            _taipei("2026-04-01 09:30"),
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [104.0, 105.0, 106.0],
            "volume": [10.0, 20.0, 30.0],
        },
        index=index,
    )
    frame.index.name = "ts"

    result = resample_session_ohlcv(frame, 60 * 60, TAIFEX_INDEX_CALENDAR)

    assert list(result.index) == [_taipei("2026-04-01 08:45")]
    assert result.iloc[0].to_dict() == {
        "open": 100.0,
        "high": 107.0,
        "low": 99.0,
        "close": 106.0,
        "volume": 60.0,
    }


def test_daily_taifex_bar_starts_at_prior_exchange_sessions_night_open() -> None:
    index = pd.DatetimeIndex(
        [
            _taipei("2026-04-10 15:00"),
            _taipei("2026-04-13 08:45"),
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [10.0, 20.0],
        },
        index=index,
    )

    result = resample_session_ohlcv(frame, 24 * 60 * 60, TAIFEX_INDEX_CALENDAR)

    assert list(result.index) == [_taipei("2026-04-10 15:00")]
    assert result.iloc[0]["volume"] == pytest.approx(30.0)


def test_daily_taifex_bar_ends_with_the_last_day_minute() -> None:
    index = pd.DatetimeIndex(
        [
            _taipei("2026-04-10 15:00"),
            _taipei("2026-04-13 13:44"),
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 103.0],
            "volume": [10.0, 920.0],
        },
        index=index,
    )

    result = resample_session_ohlcv(frame, 24 * 60 * 60, TAIFEX_INDEX_CALENDAR)

    assert list(result.index) == [_taipei("2026-04-10 15:00")]
    assert result.iloc[0]["close"] == pytest.approx(103.0)
    assert result.iloc[0]["volume"] == pytest.approx(930.0)


def test_daily_taifex_bar_closes_after_following_regular_session() -> None:
    assert bar_close(
        _taipei("2026-04-10 15:00"),
        24 * 60 * 60,
        TAIFEX_INDEX_CALENDAR,
    ) == _taipei("2026-04-13 13:45")


@pytest.mark.parametrize("close", ["2026-04-11 05:00", "2026-04-13 13:45"])
def test_session_label_excludes_a_segment_close_instant(close: str) -> None:
    with pytest.raises(ValueError, match="outside"):
        session_label(_taipei(close), TAIFEX_INDEX_CALENDAR)


@pytest.mark.parametrize(
    ("executed_at", "session"),
    [
        ("2026-04-13 08:45:00", date(2026, 4, 13)),
        # Stamped at a segment close, as a closing-auction match is.
        ("2026-04-13 13:45:00", date(2026, 4, 13)),
        ("2026-04-14 05:00:00", date(2026, 4, 14)),
        # Between segments, the session that last traded.
        ("2026-04-13 13:45:01", date(2026, 4, 13)),
        ("2026-04-14 05:00:01", date(2026, 4, 14)),
        ("2026-04-11 22:40:00", date(2026, 4, 13)),
        # Lunar New Year: the night before 02-23 opened on 02-11.
        ("2026-02-14 12:00:00", date(2026, 2, 23)),
        # Inside a segment, the bar session.
        ("2026-04-13 15:00:00", date(2026, 4, 14)),
    ],
)
def test_execution_session_counts_a_close_toward_the_session_it_ended(
    executed_at: str, session: date
) -> None:
    assert execution_session_label(_taipei(executed_at), TAIFEX_INDEX_CALENDAR) == session


@pytest.mark.parametrize(
    ("executed_at", "calendar_id", "session"),
    [
        ("2025-01-03 21:00Z", "XNYS", date(2025, 1, 3)),
        ("2025-01-04 12:00Z", "XNYS", date(2025, 1, 3)),
        # Monday before the open, a UTC date the session has not opened on yet.
        ("2025-01-06 09:00Z", "XNYS", date(2025, 1, 3)),
        # Hong Kong's lunch break.
        ("2025-01-06 04:30Z", "XHKG", date(2025, 1, 6)),
    ],
)
def test_execution_session_on_exchange_calendars(
    executed_at: str, calendar_id: str, session: date
) -> None:
    assert execution_session_label(pd.Timestamp(executed_at), calendar_id) == session


def test_execution_session_logs_an_instant_after_a_close_only(caplog) -> None:
    with caplog.at_level("WARNING"):
        execution_session_label(_taipei("2026-04-13 13:45:00"), TAIFEX_INDEX_CALENDAR)
    assert "after a XTAIFEX close" not in caplog.text

    with caplog.at_level("WARNING"):
        execution_session_label(_taipei("2026-04-13 13:45:01"), TAIFEX_INDEX_CALENDAR)
    assert "after a XTAIFEX close" in caplog.text


def test_last_minute_of_a_segment_closes_at_the_segment_close() -> None:
    assert bar_close(
        _taipei("2026-04-13 13:44"),
        60,
        TAIFEX_INDEX_CALENDAR,
    ) == _taipei("2026-04-13 13:45")


def test_resample_drops_a_print_at_the_close_instead_of_a_stub_bar(caplog) -> None:
    index = pd.DatetimeIndex(
        [
            _taipei("2026-04-13 12:44"),
            _taipei("2026-04-13 13:44"),
            _taipei("2026-04-13 13:45"),
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 102.0, 103.0],
            "high": [101.0, 103.0, 103.0],
            "low": [99.0, 101.0, 103.0],
            "close": [100.5, 102.5, 103.0],
            "volume": [10.0, 900.0, 1.0],
        },
        index=index,
    )
    frame.index.name = "ts"

    with caplog.at_level("WARNING"):
        result = resample_session_ohlcv(frame, 60 * 60, TAIFEX_INDEX_CALENDAR)

    assert list(result.index) == [_taipei("2026-04-13 11:45"), _taipei("2026-04-13 12:45")]
    assert result.loc[_taipei("2026-04-13 12:45")]["close"] == pytest.approx(102.5)
    assert "dropped 1 bar" in caplog.text


def test_resample_drops_isolated_off_session_bar_instead_of_raising(caplog) -> None:
    """Reproduces a real Shioaji TXFR1 print: a single-lot bar on a Saturday
    evening with no session anywhere near it (previous_session's night
    segment closes Saturday 05:00; the next session is the following
    Monday) — this must be dropped with a warning, not fail the whole
    resample."""
    index = pd.DatetimeIndex(
        [
            _taipei("2026-04-13 13:43"),
            _taipei("2026-04-13 13:44"),
            _taipei("2026-04-18 22:40"),  # isolated: Saturday, no session covers this
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 103.0],
            "high": [101.0, 103.0, 103.0],
            "low": [99.0, 103.0, 103.0],
            "close": [100.5, 103.0, 103.0],
            "volume": [10.0, 900.0, 1.0],
        },
        index=index,
    )
    frame.index.name = "ts"

    with caplog.at_level("WARNING"):
        result = resample_session_ohlcv(frame, 60, TAIFEX_INDEX_CALENDAR)

    assert list(result.index) == [
        _taipei("2026-04-13 13:43"),
        _taipei("2026-04-13 13:44"),
    ]
    assert "dropped 1 bar" in caplog.text


def test_resample_raises_when_outliers_look_systemic_not_isolated() -> None:
    """A wrong calendar_id or a broken upstream timestamp normalization
    produces many out-of-session bars, not a handful — that must fail loudly
    instead of silently returning a truncated result."""
    valid = [
        _taipei("2026-04-13 09:00"),
        _taipei("2026-04-13 09:01"),
    ]
    off_session = [_taipei("2026-04-18 22:40") + pd.Timedelta(minutes=i) for i in range(8)]
    index = pd.DatetimeIndex(valid + off_session)
    frame = pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [100.0] * len(index),
            "low": [100.0] * len(index),
            "close": [100.0] * len(index),
            "volume": [1.0] * len(index),
        },
        index=index,
    )
    frame.index.name = "ts"

    with pytest.raises(ValueError, match="too many to treat as isolated noise"):
        resample_session_ohlcv(frame, 60, TAIFEX_INDEX_CALENDAR)


def test_resampling_rejects_intervals_longer_than_one_session() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0],
            "high": [100.0],
            "low": [100.0],
            "close": [100.0],
            "volume": [1.0],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-04-01 00:00Z")]),
    )

    with pytest.raises(ValueError, match="up to D1"):
        resample_session_ohlcv(frame, 7 * 24 * 60 * 60, "24/7")


@pytest.mark.parametrize(
    ("instant", "calendar_id", "expected"),
    [
        # Around the night close: the day segment of the same session is next.
        ("2026-11-03 04:59", TAIFEX_INDEX_CALENDAR, "2026-11-03 08:45"),
        ("2026-11-03 05:00", TAIFEX_INDEX_CALENDAR, "2026-11-03 08:45"),
        ("2026-11-03 05:01", TAIFEX_INDEX_CALENDAR, "2026-11-03 08:45"),
        ("2026-11-03 08:44", TAIFEX_INDEX_CALENDAR, "2026-11-03 08:45"),
        ("2026-11-03 08:45", TAIFEX_INDEX_CALENDAR, "2026-11-03 08:45"),
        ("2026-11-03 13:45", TAIFEX_INDEX_CALENDAR, "2026-11-03 15:00"),
        ("2026-11-03 15:00", TAIFEX_INDEX_CALENDAR, "2026-11-03 15:00"),
        ("2026-11-03 13:45", TAIFEX_LATE_OPEN_CALENDAR, "2026-11-03 17:25"),
        # Friday's night closes Saturday 05:00; Monday's day segment is next.
        ("2026-11-07 05:00", TAIFEX_INDEX_CALENDAR, "2026-11-09 08:45"),
        ("2026-11-08 12:00", TAIFEX_INDEX_CALENDAR, "2026-11-09 08:45"),
        # 2026-01-01 is a holiday: the night before it closes into 01-02.
        ("2026-01-01 05:00", TAIFEX_INDEX_CALENDAR, "2026-01-02 08:45"),
        ("2026-01-01 12:00", TAIFEX_INDEX_CALENDAR, "2026-01-02 08:45"),
        # Hong Kong resumes after lunch, not at the next session.
        ("2026-11-03 12:00", "XHKG", "2026-11-03 13:00"),
        ("2026-11-03 16:00", "XHKG", "2026-11-04 09:30"),
    ],
)
def test_next_segment_open_after(instant: str, calendar_id: str, expected: str) -> None:
    timezone = "Asia/Hong_Kong" if calendar_id == "XHKG" else "Asia/Taipei"

    def local(value: str) -> pd.Timestamp:
        return pd.Timestamp(value, tz=timezone).tz_convert("UTC")

    assert next_segment_open_after(local(instant), calendar_id) == local(expected)


@pytest.mark.parametrize(
    "calendar_id",
    [TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR, "XTAI", "XHKG", "XNYS", "24/7"],
)
def test_next_segment_open_is_never_before_its_input(calendar_id: str) -> None:
    for instant in pd.date_range("2026-11-02", "2026-11-10", freq="7min", tz="UTC"):
        assert next_segment_open_after(instant, calendar_id) >= instant


def _traded_days() -> tuple[date, list[date]]:
    """Last day the fixture covers, and every day the exchanges traded until then."""
    days = []
    for line in Path(__file__).with_name("xtai_trading_days.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            month, *dates = line.split()
            days += [date.fromisoformat(f"{month}-{day}") for day in dates]
    return days[-1], days


@pytest.mark.parametrize("calendar_id", ["XTAI", TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR])
def test_sessions_are_the_days_the_exchanges_traded(calendar_id: str) -> None:
    last, traded = _traded_days()
    sessions = _exchange_calendar(calendar_id).sessions
    assert [session.date() for session in sessions[sessions <= pd.Timestamp(last)]] == traded


@cache
def _upstream_xtai():
    from exchange_calendars.exchange_calendar_xtai import XTAIExchangeCalendar

    last = max(row.day for row in _XTAI_OVERRIDES)
    return XTAIExchangeCalendar(start="2000-01-01", end=last + timedelta(days=14))


@pytest.mark.parametrize("row", _XTAI_OVERRIDES, ids=lambda row: f"{row.day}-{row.kind}")
def test_an_override_is_still_missing_upstream(row) -> None:
    """Once an exchange_calendars release has the row, drop it from the table."""
    is_session = pd.Timestamp(row.day) in _upstream_xtai().sessions
    assert is_session == (row.kind == "closed")


@pytest.mark.parametrize("row", _XTAI_OVERRIDES, ids=lambda row: f"{row.day}-{row.kind}")
def test_an_override_takes_effect(row) -> None:
    is_session = pd.Timestamp(row.day) in _exchange_calendar("XTAI").sessions
    assert is_session == (row.kind == "opened")


def test_every_override_is_sourced_and_unique() -> None:
    assert len({row.day for row in _XTAI_OVERRIDES}) == len(_XTAI_OVERRIDES)
    for row in _XTAI_OVERRIDES:
        assert row.sources, row
        assert all(source.startswith("https://") for source in row.sources), row


@pytest.mark.parametrize("calendar_id", ["XNYS", "XHKG"])
def test_a_calendar_without_overrides_is_upstreams(calendar_id: str) -> None:
    import exchange_calendars as xcals

    upstream = xcals.get_calendar(calendar_id, start="2000-01-01")
    assert _exchange_calendar(calendar_id).schedule.equals(upstream.schedule)


_ANNOUNCED_CLOSURES = ["2022-02-04 10:00", "2023-01-18 10:00", "2026-10-23 10:00"]


@pytest.mark.parametrize("instant", _ANNOUNCED_CLOSURES)
@pytest.mark.parametrize("calendar_id", ["XTAI", TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR])
def test_an_announced_closure_is_not_a_session(calendar_id: str, instant: str) -> None:
    with pytest.raises(ValueError):
        session_label(_taipei(instant), calendar_id)


# Saturdays in TAIFEX's daily futures records.
_KNOWN_SATURDAY_SESSIONS = ["2006-10-14", "2007-03-03", "2007-04-14", "2010-02-06", "2012-02-04"]


@pytest.mark.parametrize("day", _KNOWN_SATURDAY_SESSIONS)
@pytest.mark.parametrize("calendar_id", ["XTAI", TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR])
def test_a_traded_saturday_is_a_session(calendar_id: str, day: str) -> None:
    assert session_label(_taipei(f"{day} 10:00"), calendar_id) == date.fromisoformat(day)


def test_the_night_before_a_traded_saturday_belongs_to_it() -> None:
    assert session_label(_taipei("2017-06-02 16:00"), TAIFEX_INDEX_CALENDAR) == date(2017, 6, 3)
    assert session_label(_taipei("2017-06-05 09:00"), TAIFEX_INDEX_CALENDAR) == date(2017, 6, 5)


# Saturdays traded after the night session launched; TAIFEX's daily futures
# report shows the night before each and none after it.
_NIGHT_ERA_SATURDAYS = [
    row.day
    for row in _XTAI_OVERRIDES
    if row.kind == "opened" and row.day.weekday() == 5 and row.day >= date(2017, 5, 15)
]


def test_the_night_era_saturdays_are_the_ones_checked_against_taifex() -> None:
    assert [
        date(2017, 6, 3),
        date(2017, 9, 30),
        date(2018, 3, 31),
        date(2018, 12, 22),
    ] == _NIGHT_ERA_SATURDAYS


@pytest.mark.parametrize("saturday", _NIGHT_ERA_SATURDAYS, ids=str)
@pytest.mark.parametrize(
    ("calendar_id", "night_open"),
    [(TAIFEX_INDEX_CALENDAR, "15:00"), (TAIFEX_LATE_OPEN_CALENDAR, "17:25")],
)
def test_no_night_opens_on_a_traded_saturday(
    calendar_id: str, night_open: str, saturday: date
) -> None:
    friday = saturday - timedelta(days=1)
    monday = saturday + timedelta(days=2)
    assert session_bounds(saturday, calendar_id) == (
        _taipei(f"{friday} {night_open}"),
        _taipei(f"{saturday} 13:45"),
    )
    assert session_bounds(monday, calendar_id) == (
        _taipei(f"{monday} 08:45"),
        _taipei(f"{monday} 13:45"),
    )
    assert _session_table(calendar_id).loc[monday, "open"] == _taipei(f"{monday} 08:45")
    with pytest.raises(ValueError, match="outside"):
        session_label(_taipei(f"{saturday} 20:00"), calendar_id)


@pytest.mark.parametrize("calendar_id", ["XTAI", TAIFEX_INDEX_CALENDAR])
def test_an_untraded_saturday_is_not_a_session(calendar_id: str) -> None:
    with pytest.raises(ValueError, match="outside"):
        session_label(_taipei("2010-03-06 10:00"), calendar_id)


@pytest.mark.parametrize("calendar_id", ["XTAI", TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR])
def test_a_closure_that_upstream_already_has_stays_closed(calendar_id: str) -> None:
    # 2023-01-19 is marked like 01-18 on the exchange's table and needs no entry.
    with pytest.raises(ValueError):
        session_label(_taipei("2023-01-19 10:00"), calendar_id)


def test_a_night_segment_before_a_closure_counts_toward_the_next_session() -> None:
    # TAIFEX ran the 10-22 night segment, then closed 10-23; 10-26 is a holiday.
    assert session_label(_taipei("2026-10-22 20:00"), TAIFEX_INDEX_CALENDAR) == date(2026, 10, 27)
    assert next_segment_open_after(_taipei("2026-10-23 05:00"), TAIFEX_INDEX_CALENDAR) == _taipei(
        "2026-10-27 08:45"
    )


@pytest.mark.parametrize("calendar_id", [TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR])
@pytest.mark.parametrize(
    ("night", "next_session"),
    [
        # 01-27 and 01-28 are closed upstream and 02-04 by this list: next is 02-07.
        ("2022-01-26 20:00", date(2022, 2, 7)),
        # 01-18 is closed by this list, 01-19 and the holidays upstream: next is 01-30.
        ("2023-01-17 20:00", date(2023, 1, 30)),
    ],
)
def test_the_night_before_a_lunar_new_year_closure_belongs_to_the_session_after_it(
    calendar_id: str, night: str, next_session: date
) -> None:
    assert session_label(_taipei(night), calendar_id) == next_session


@pytest.mark.parametrize(
    ("calendar_id", "label", "first_open", "final_close"),
    [
        ("XTAI", "2000-12-29", "2000-12-29 09:00", "2000-12-29 12:00"),
        ("XTAI", "2000-12-30", "2000-12-30 09:00", "2000-12-30 12:00"),
        ("XTAI", "2001-01-02", "2001-01-02 09:00", "2001-01-02 13:30"),
        (TAIFEX_INDEX_CALENDAR, "2000-12-29", "2000-12-29 09:00", "2000-12-29 12:15"),
        (TAIFEX_INDEX_CALENDAR, "2000-12-30", "2000-12-30 09:00", "2000-12-30 12:15"),
        (TAIFEX_INDEX_CALENDAR, "2001-01-02", "2001-01-02 08:45", "2001-01-02 13:45"),
        # The night session first opened at 15:00 on 2017-05-15, for 05-16.
        (TAIFEX_INDEX_CALENDAR, "2017-05-15", "2017-05-15 08:45", "2017-05-15 13:45"),
        (TAIFEX_INDEX_CALENDAR, "2017-05-16", "2017-05-15 15:00", "2017-05-16 13:45"),
        (TAIFEX_LATE_OPEN_CALENDAR, "2000-12-30", "2000-12-30 09:00", "2000-12-30 12:15"),
        (TAIFEX_LATE_OPEN_CALENDAR, "2017-05-15", "2017-05-15 08:45", "2017-05-15 13:45"),
        (TAIFEX_LATE_OPEN_CALENDAR, "2017-05-16", "2017-05-15 17:25", "2017-05-16 13:45"),
    ],
)
def test_session_bounds_follow_the_hours_in_force(
    calendar_id: str, label: str, first_open: str, final_close: str
) -> None:
    bounds = session_bounds(date.fromisoformat(label), calendar_id)
    assert bounds == (_taipei(first_open), _taipei(final_close))


@pytest.mark.parametrize(
    ("calendar_id", "instant"),
    [
        ("XTAI", "2000-12-29 12:00"),
        (TAIFEX_INDEX_CALENDAR, "2000-12-29 08:45"),
        (TAIFEX_INDEX_CALENDAR, "2000-12-29 12:15"),
        (TAIFEX_INDEX_CALENDAR, "2017-05-12 20:00"),
        (TAIFEX_LATE_OPEN_CALENDAR, "2017-05-12 20:00"),
    ],
)
def test_an_instant_outside_the_hours_then_in_force_is_out_of_session(
    calendar_id: str, instant: str
) -> None:
    with pytest.raises(ValueError, match="outside"):
        session_label(_taipei(instant), calendar_id)


@pytest.mark.parametrize(
    ("calendar_id", "session_mode"),
    [
        (calendar_id, session_mode)
        for calendar_id in _SESSION_HOURS
        for session_mode in ("regular", "extended", "overnight")
        if session_mode != "overnight" or calendar_id.startswith("XTAIFEX")
    ],
)
def test_the_session_table_matches_each_sessions_segments(
    calendar_id: str, session_mode: str
) -> None:
    table = _session_table(calendar_id, session_mode)
    sessions = _exchange_calendar(calendar_id).sessions
    for start in ("2000-12-20", "2017-05-05", "2017-09-25", "2018-07-05", "2018-12-17"):
        for session in sessions[sessions >= pd.Timestamp(start)][:15]:
            label = session.date()
            segments = _session_segments(calendar_id, label, session_mode)
            if not segments:
                assert label not in table.index, label
                continue
            row = table.loc[label]
            later_open = None if pd.isna(row["later_open"]) else row["later_open"]
            assert (row["open"], row["close"]) == (segments[0][0], segments[-1][1]), label
            assert later_open == (segments[1][0] if len(segments) > 1 else None), label


@pytest.mark.parametrize("calendar_id", list(_SESSION_HOURS))
def test_every_hours_row_is_sourced_and_in_order(calendar_id: str) -> None:
    rows = _SESSION_HOURS[calendar_id]
    assert rows[0].start is None
    starts = [row.start for row in rows[1:]]
    assert starts == sorted(set(starts))
    # _session_segments relies on a row never dropping a night segment.
    nights = [row.night is not None for row in rows]
    assert nights == sorted(nights)
    for row in rows:
        assert row.sources, row
        assert all(source.startswith("https://") for source in row.sources), row


# A TAIFEX daily bar in each session mode: an instant in the session, the bar
# start and its close (Taipei). ``None`` when the session has no segment in the
# mode, so no bar of that series holds it.
_TAIFEX_DAILY_BARS = [
    # Before 2001: 09:00-12:15, and no night.
    ("regular", "2000-12-29 10:00", ("2000-12-29 09:00", "2000-12-29 12:15")),
    ("extended", "2000-12-29 10:00", ("2000-12-29 09:00", "2000-12-29 12:15")),
    ("overnight", "2000-12-29 10:00", None),
    ("regular", "2001-01-02 10:00", ("2001-01-02 08:45", "2001-01-02 13:45")),
    # The first night opened at 15:00 on 2017-05-15 and counts toward 05-16.
    ("overnight", "2017-05-15 10:00", None),
    ("overnight", "2017-05-15 15:30", ("2017-05-15 15:00", "2017-05-16 05:00")),
    ("regular", "2017-05-15 15:30", ("2017-05-16 08:45", "2017-05-16 13:45")),
    ("extended", "2017-05-15 15:30", ("2017-05-15 15:00", "2017-05-16 13:45")),
    ("extended", "2017-05-16 09:00", ("2017-05-15 15:00", "2017-05-16 13:45")),
    # A traded Saturday keeps the Friday night; no night opens on it, so the
    # Monday after has a day segment only.
    ("overnight", "2018-12-21 16:00", ("2018-12-21 15:00", "2018-12-22 05:00")),
    ("regular", "2018-12-22 10:00", ("2018-12-22 08:45", "2018-12-22 13:45")),
    ("overnight", "2018-12-24 09:00", None),
    ("extended", "2018-12-24 09:00", ("2018-12-24 08:45", "2018-12-24 13:45")),
    # Closed Friday 2026-07-10: the Thursday night counts toward Monday and
    # still closes at 05:00 the next calendar day.
    ("overnight", "2026-07-09 20:00", ("2026-07-09 15:00", "2026-07-10 05:00")),
    ("regular", "2026-07-13 09:00", ("2026-07-13 08:45", "2026-07-13 13:45")),
    ("extended", "2026-07-09 20:00", ("2026-07-09 15:00", "2026-07-13 13:45")),
]


def _assert_daily_bar(
    calendar_id: str, session_mode: str, instant: str, bar: tuple[str, str] | None
) -> None:
    timestamp = _taipei(instant)
    if bar is None:
        with pytest.raises(ValueError, match=f"no {session_mode} segment"):
            period_start(timestamp, "D1", calendar_id, session_mode=session_mode)
        return
    start, close = (_taipei(value) for value in bar)
    assert period_start(timestamp, "D1", calendar_id, session_mode=session_mode) == start
    assert period_close(start, "D1", calendar_id, session_mode=session_mode) == close
    assert bar_start_problem(start, "D1", calendar_id, session_mode=session_mode) is None
    # The bar is labelled with its trading day in every mode.
    assert session_label(start, calendar_id) == session_label(timestamp, calendar_id)


@pytest.mark.parametrize(("session_mode", "instant", "bar"), _TAIFEX_DAILY_BARS)
def test_a_taifex_daily_bar_holds_the_segments_of_its_session_mode(
    session_mode: str, instant: str, bar: tuple[str, str] | None
) -> None:
    _assert_daily_bar(TAIFEX_INDEX_CALENDAR, session_mode, instant, bar)


# TAIFEX cancelled the night of 2018-07-10; the session of 07-11 traded its day
# segment only, and the nights either side of it ran. ``{night}`` is the
# calendar's night open.
_CANCELLED_NIGHT_BARS = [
    ("overnight", "2018-07-10 09:00", ("2018-07-09 {night}", "2018-07-10 05:00")),
    ("overnight", "2018-07-11 09:00", None),
    ("regular", "2018-07-11 09:00", ("2018-07-11 08:45", "2018-07-11 13:45")),
    ("extended", "2018-07-11 09:00", ("2018-07-11 08:45", "2018-07-11 13:45")),
    ("overnight", "2018-07-11 20:00", ("2018-07-11 {night}", "2018-07-12 05:00")),
    ("extended", "2018-07-11 20:00", ("2018-07-11 {night}", "2018-07-12 13:45")),
]


@pytest.mark.parametrize(
    ("calendar_id", "night_open"),
    [(TAIFEX_INDEX_CALENDAR, "15:00"), (TAIFEX_LATE_OPEN_CALENDAR, "17:25")],
)
@pytest.mark.parametrize(("session_mode", "instant", "bar"), _CANCELLED_NIGHT_BARS)
def test_a_cancelled_night_is_missing_from_its_sessions_daily_bars(
    calendar_id: str,
    night_open: str,
    session_mode: str,
    instant: str,
    bar: tuple[str, str] | None,
) -> None:
    resolved = None if bar is None else tuple(value.format(night=night_open) for value in bar)
    _assert_daily_bar(calendar_id, session_mode, instant, resolved)


def test_every_cancelled_night_is_sourced_and_precedes_a_session_with_a_night() -> None:
    for day, sources in _CANCELLED_NIGHTS.items():
        assert sources, day
        assert all(source.startswith("https://") for source in sources), day
        for calendar_id in (TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR):
            sessions = _exchange_calendar(calendar_id).sessions
            assert pd.Timestamp(day) in sessions, day
            following = sessions[sessions.get_loc(pd.Timestamp(day)) + 1].date()
            assert _hours(calendar_id, following).night is not None, day
            # A weekday on which a night would open anyway, else the row is idle.
            assert day.weekday() in _hours(calendar_id, day).night_weekdays, day


@pytest.mark.parametrize("calendar_id", [TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR])
def test_a_cancelled_night_leaves_its_session_the_day_segment_only(calendar_id: str) -> None:
    label = date(2018, 7, 11)
    assert _session_segments(calendar_id, label, "overnight") == ()
    assert label not in _session_table(calendar_id, "overnight").index
    (day,) = _session_segments(calendar_id, label, "regular")
    assert _session_segments(calendar_id, label, "extended") == (day,)
    assert _session_table(calendar_id, "extended").loc[label, "open"] == day[0]
    # Neighbouring sessions keep their nights.
    for neighbour in (date(2018, 7, 10), date(2018, 7, 12)):
        assert len(_session_segments(calendar_id, neighbour, "overnight")) == 1


def test_the_late_open_calendar_has_its_own_night() -> None:
    start = period_start(
        _taipei("2017-05-15 18:00"), "D1", TAIFEX_LATE_OPEN_CALENDAR, session_mode="overnight"
    )
    assert start == _taipei("2017-05-15 17:25")
    assert period_close(
        start, "D1", TAIFEX_LATE_OPEN_CALENDAR, session_mode="overnight"
    ) == _taipei("2017-05-16 05:00")


@pytest.mark.parametrize(
    ("session_mode", "stamp", "problem"),
    [
        ("regular", "2026-11-12 08:45", None),
        ("regular", "2026-11-11 15:00", "its D1 bar starts at 2026-11-12T00:45:00+00:00"),
        ("overnight", "2026-11-11 15:00", None),
        ("overnight", "2026-11-12 08:45", "its D1 bar starts at 2026-11-11T07:00:00+00:00"),
        ("extended", "2026-11-11 15:00", None),
        ("extended", "2026-11-12 08:45", "its D1 bar starts at 2026-11-11T07:00:00+00:00"),
    ],
)
def test_a_taifex_daily_bar_starts_where_its_session_mode_opens(
    session_mode: str, stamp: str, problem: str | None
) -> None:
    assert (
        bar_start_problem(_taipei(stamp), "D1", TAIFEX_INDEX_CALENDAR, session_mode=session_mode)
        == problem
    )


@pytest.mark.parametrize(
    ("session_mode", "stamp"),
    [("regular", "2026-11-11 20:00"), ("overnight", "2026-11-12 09:00")],
)
def test_an_intraday_bar_outside_the_modes_segments_is_refused(
    session_mode: str, stamp: str
) -> None:
    timestamp = _taipei(stamp)
    assert (
        bar_start_problem(timestamp, "M1", TAIFEX_INDEX_CALENDAR, session_mode="extended") is None
    )
    assert (
        bar_start_problem(timestamp, "M1", TAIFEX_INDEX_CALENDAR, session_mode=session_mode)
        == "outside the XTAIFEX trading sessions"
    )


def test_a_contracts_settlement_day_has_a_day_bar_and_the_night_before_it() -> None:
    """The expiring contract stops trading in its settlement day's day
    segment, so its last night is the one before; the night that opens on the
    settlement day counts toward the next session. The calendar carries no
    contract, so the expiring contract's earlier last-trade close is not here."""
    from librae.core.taifex import taifex_final_settlement_day

    settlement = taifex_final_settlement_day("202611")
    assert settlement == date(2026, 11, 18)
    day_bar = period_start(
        _taipei("2026-11-18 10:00"), "D1", TAIFEX_INDEX_CALENDAR, session_mode="regular"
    )
    assert day_bar == _taipei("2026-11-18 08:45")
    night_before = period_start(
        _taipei("2026-11-18 10:00"), "D1", TAIFEX_INDEX_CALENDAR, session_mode="overnight"
    )
    assert night_before == _taipei("2026-11-17 15:00")
    night_after = _taipei("2026-11-18 15:00")
    assert session_label(night_after, TAIFEX_INDEX_CALENDAR) == date(2026, 11, 19)


@pytest.mark.parametrize(
    ("timeframe", "instant", "start", "close"),
    [
        # The week of the 2018-12-22 Saturday: its first night is Monday's,
        # which opens the Friday before, and its last is the Friday night the
        # Saturday keeps.
        ("W1", "2018-12-20 20:00", "2018-12-14 15:00", "2018-12-22 05:00"),
        # May 2017's first night opened on 05-15. Its last session, 05-31,
        # follows a holiday, so its night opened 05-26 and closed 05-27.
        ("MN1", "2017-05-22 20:00", "2017-05-15 15:00", "2017-05-27 05:00"),
    ],
)
def test_a_taifex_overnight_period_spans_the_nights_labelled_in_it(
    timeframe: str, instant: str, start: str, close: str
) -> None:
    opened = period_start(
        _taipei(instant), timeframe, TAIFEX_INDEX_CALENDAR, session_mode="overnight"
    )
    assert opened == _taipei(start)
    assert period_close(
        opened, timeframe, TAIFEX_INDEX_CALENDAR, session_mode="overnight"
    ) == _taipei(close)


@pytest.mark.parametrize("calendar_id", ["XNYS", "XTAI", "24/7"])
def test_overnight_needs_a_calendar_that_defines_them(calendar_id: str) -> None:
    with pytest.raises(ValueError, match="overnight"):
        validate_session_mode(calendar_id, "overnight")
    validate_session_mode(calendar_id, "regular")
    validate_session_mode(TAIFEX_INDEX_CALENDAR, "overnight")
    with pytest.raises(ValueError, match="session_mode must be one of"):
        validate_session_mode(TAIFEX_INDEX_CALENDAR, "night")
