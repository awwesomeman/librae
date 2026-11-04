from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from librae.core.trading_calendar import (
    _XTAI_CLOSURES_MISSING_UPSTREAM,
    TAIFEX_INDEX_CALENDAR,
    TAIFEX_LATE_OPEN_CALENDAR,
    bar_close,
    execution_session_label,
    next_segment_open_after,
    resample_session_ohlcv,
    session_label,
    session_lookback_days,
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
        ("XNYS", "1990-01-03 14:30Z"),
        ("XTAI", "1990-01-03 02:00Z"),
        (TAIFEX_INDEX_CALENDAR, "1990-01-03 02:00Z"),
    ],
)
def test_calendars_reach_back_past_the_library_default(calendar_id: str, instant: str) -> None:
    """exchange_calendars builds only the last 20 years unless given a start."""
    assert session_label(pd.Timestamp(instant), calendar_id) == date(1990, 1, 3)


def test_a_calendar_that_cannot_reach_back_that_far_still_builds() -> None:
    # XTKS records holidays only from 1997.
    assert session_label(pd.Timestamp("2026-11-04 01:00Z"), "XTKS") == date(2026, 11, 4)


def test_xtai_and_the_taifex_calendars_share_one_calendar() -> None:
    from librae.core.trading_calendar import _exchange_calendar

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


@pytest.mark.parametrize("closure", _XTAI_CLOSURES_MISSING_UPSTREAM)
def test_announced_closures_missing_upstream_are_still_missing(closure: date) -> None:
    """Once an exchange_calendars release has the date, drop it from the list."""
    import exchange_calendars as xcals

    assert xcals.get_calendar("XTAI").is_session(pd.Timestamp(closure))


_ANNOUNCED_CLOSURES = ["2022-02-04 10:00", "2023-01-18 10:00", "2026-10-23 10:00"]


@pytest.mark.parametrize("instant", _ANNOUNCED_CLOSURES)
@pytest.mark.parametrize("calendar_id", ["XTAI", TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR])
def test_an_announced_closure_is_not_a_session(calendar_id: str, instant: str) -> None:
    with pytest.raises(ValueError):
        session_label(_taipei(instant), calendar_id)


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
