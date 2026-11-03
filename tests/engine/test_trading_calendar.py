from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from librae.core.trading_calendar import (
    TAIFEX_INDEX_CALENDAR,
    TAIFEX_LATE_OPEN_CALENDAR,
    bar_close,
    execution_session_label,
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
