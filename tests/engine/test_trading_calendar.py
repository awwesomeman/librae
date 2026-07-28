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


def test_daily_taifex_bar_closes_after_following_regular_session() -> None:
    assert bar_close(
        _taipei("2026-04-10 15:00"),
        24 * 60 * 60,
        TAIFEX_INDEX_CALENDAR,
    ) == _taipei("2026-04-13 13:45")


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
