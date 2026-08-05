from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from librae.core.trading_calendar import (
    TAIFEX_INDEX_CALENDAR,
    TAIFEX_LATE_OPEN_CALENDAR,
    bar_close,
    resample_session_ohlcv,
    session_label,
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


def test_daily_taifex_close_is_the_closing_auction_print() -> None:
    index = pd.DatetimeIndex(
        [
            _taipei("2026-04-10 15:00"),
            _taipei("2026-04-13 13:44"),
            _taipei("2026-04-13 13:45"),
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0, 103.0],
            "high": [102.0, 103.0, 103.0],
            "low": [99.0, 100.0, 103.0],
            "close": [101.0, 102.5, 103.0],
            "volume": [10.0, 20.0, 900.0],
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


def test_session_label_includes_night_session_close_instant() -> None:
    assert session_label(
        _taipei("2026-04-11 05:00"),
        TAIFEX_INDEX_CALENDAR,
    ) == date(2026, 4, 13)


def test_session_label_includes_day_session_close_instant() -> None:
    assert session_label(
        _taipei("2026-04-13 13:45"),
        TAIFEX_INDEX_CALENDAR,
    ) == date(2026, 4, 13)


def test_resample_keeps_closing_auction_print_as_final_bucket() -> None:
    index = pd.DatetimeIndex(
        [
            _taipei("2026-04-13 13:44"),
            _taipei("2026-04-13 13:45"),
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 103.0],
            "high": [101.0, 103.0],
            "low": [99.0, 103.0],
            "close": [100.5, 103.0],
            "volume": [10.0, 900.0],
        },
        index=index,
    )
    frame.index.name = "ts"

    result = resample_session_ohlcv(frame, 60, TAIFEX_INDEX_CALENDAR)

    assert list(result.index) == [
        _taipei("2026-04-13 13:44"),
        _taipei("2026-04-13 13:45"),
    ]
    assert result.loc[_taipei("2026-04-13 13:45")]["volume"] == pytest.approx(900.0)


def test_bar_close_of_closing_auction_bucket_is_itself() -> None:
    assert bar_close(
        _taipei("2026-04-13 13:45"),
        60,
        TAIFEX_INDEX_CALENDAR,
    ) == _taipei("2026-04-13 13:45")


def test_resample_drops_isolated_off_session_bar_instead_of_raising(caplog) -> None:
    """Reproduces a real Shioaji TXFR1 print: a single-lot bar on a Saturday
    evening with no session anywhere near it (previous_session's night
    segment closes Saturday 05:00; the next session is the following
    Monday) — this must be dropped with a warning, not fail the whole
    resample."""
    index = pd.DatetimeIndex(
        [
            _taipei("2026-04-13 13:44"),
            _taipei("2026-04-13 13:45"),
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
        _taipei("2026-04-13 13:44"),
        _taipei("2026-04-13 13:45"),
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
