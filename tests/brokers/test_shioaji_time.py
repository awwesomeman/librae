"""Tests for Shioaji's vendor-specific timestamp correction."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from librae.brokers.base import SEGMENT_END_SETTLE, with_settled_available_at
from librae.brokers.shioaji_time import fold_closing_minutes, shioaji_kbar_starts
from librae.core.taifex import taifex_final_settlement_day
from librae.core.trading_calendar import TAIFEX_INDEX_CALENDAR


def test_minute_end_in_fake_utc_becomes_the_utc_minute_start() -> None:
    raw_ns = pd.Timestamp("2026-11-02 08:46", tz="UTC").value

    starts = shioaji_kbar_starts([raw_ns])

    assert list(starts) == [pd.Timestamp("2026-11-02 08:45", tz="Asia/Taipei")]
    assert str(starts.tz) == "UTC"


def _taipei(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="Asia/Taipei").tz_convert("UTC")


def _minutes(*rows: tuple[str, float, int]) -> pd.DataFrame:
    """Minute bars from (Taipei start, price, volume) rows, one price per bar."""
    return pd.DataFrame(
        {
            "ts": [_taipei(start) for start, _, _ in rows],
            "open": [price for _, price, _ in rows],
            "high": [price for _, price, _ in rows],
            "low": [price for _, price, _ in rows],
            "close": [price for _, price, _ in rows],
            "volume": [volume for _, _, volume in rows],
            "amount": [price * volume for _, price, volume in rows],
        }
    )


def test_day_close_trades_join_the_last_day_minute() -> None:
    minutes = _minutes(
        ("2026-11-02 13:43", 100.0, 1),
        ("2026-11-02 13:44", 101.0, 2),
        ("2026-11-02 13:45", 99.0, 3),
    )

    folded = fold_closing_minutes(minutes, TAIFEX_INDEX_CALENDAR)

    assert folded["ts"].tolist() == [_taipei("2026-11-02 13:43"), _taipei("2026-11-02 13:44")]
    last = folded.iloc[-1]
    assert (last["open"], last["high"], last["low"], last["close"]) == (101, 101, 99, 99)
    assert (last["volume"], last["amount"]) == (5, 499)
    stamped = with_settled_available_at(folded, "1m", TAIFEX_INDEX_CALENDAR)
    assert stamped["available_at"].iloc[-1] == _taipei("2026-11-02 13:45") + SEGMENT_END_SETTLE


def test_night_close_trades_join_the_last_night_minute() -> None:
    """Trading day 2026-11-02's night session ends at 05:00 on Saturday."""
    minutes = _minutes(
        ("2026-10-31 04:59", 100.0, 1),
        ("2026-10-31 05:00", 102.0, 4),
    )

    folded = fold_closing_minutes(minutes, TAIFEX_INDEX_CALENDAR)

    assert folded["ts"].tolist() == [_taipei("2026-10-31 04:59")]
    last = folded.iloc[-1]
    assert (last["open"], last["high"], last["close"], last["volume"]) == (100, 102, 102, 5)


def test_a_close_without_trades_leaves_the_minutes_as_they_are() -> None:
    minutes = _minutes(
        ("2026-11-02 13:43", 100.0, 1),
        ("2026-11-02 13:44", 101.0, 2),
    )

    folded = fold_closing_minutes(minutes, TAIFEX_INDEX_CALENDAR)

    pd.testing.assert_frame_equal(folded, minutes)


def test_a_closing_minute_with_no_minute_before_it_becomes_the_last() -> None:
    minutes = _minutes(("2026-11-02 13:40", 100.0, 1), ("2026-11-02 13:45", 99.0, 3))

    folded = fold_closing_minutes(minutes, TAIFEX_INDEX_CALENDAR)

    assert folded["ts"].tolist() == [_taipei("2026-11-02 13:40"), _taipei("2026-11-02 13:44")]
    assert folded["volume"].tolist() == [1, 3]


def test_on_a_final_settlement_day_only_the_session_close_folds() -> None:
    """The calendar keeps its 13:45 close on a final settlement day; an
    expiring contract's own 13:30 close lies inside the segment."""
    assert taifex_final_settlement_day("202611") == date(2026, 11, 18)
    minutes = _minutes(
        ("2026-11-18 13:29", 100.0, 1),
        ("2026-11-18 13:30", 101.0, 1),
        ("2026-11-18 13:44", 102.0, 1),
        ("2026-11-18 13:45", 103.0, 1),
    )

    folded = fold_closing_minutes(minutes, TAIFEX_INDEX_CALENDAR)

    assert folded["ts"].tolist() == [
        _taipei("2026-11-18 13:29"),
        _taipei("2026-11-18 13:30"),
        _taipei("2026-11-18 13:44"),
    ]
    assert folded["volume"].tolist() == [1, 1, 2]


def test_a_column_with_no_merge_rule_is_refused() -> None:
    """Summing a contract identifier would corrupt only the closing minutes."""
    minutes = _minutes(("2026-11-02 13:44", 101.0, 2)).assign(symbol="TXO")

    with pytest.raises(ValueError, match=r"\['symbol'\]"):
        fold_closing_minutes(minutes, TAIFEX_INDEX_CALENDAR)


def test_other_calendars_are_not_folded() -> None:
    minutes = _minutes(("2026-11-05 13:29", 100.0, 1), ("2026-11-05 13:30", 101.0, 1))

    assert fold_closing_minutes(minutes, "XTAI") is minutes
