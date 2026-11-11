"""Tests for shared broker-boundary helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pandas as pd
import pytest
from librae.brokers.base import (
    SEGMENT_END_SETTLE,
    drop_incomplete_ohlcv,
    settled_available_at,
    validate_order_signal,
    with_settled_available_at,
)
from librae.brokers.ibkr_adapter import IBKRAdapter
from librae.brokers.shioaji_adapter import ShioajiAdapter


def test_drop_incomplete_uses_calendar_session_close() -> None:
    frame = pd.DataFrame({"ts": [pd.Timestamp("2026-04-10 07:00Z")]})

    with patch("librae.brokers.base.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = datetime(2026, 4, 13, 4, 0, tzinfo=UTC)
        result = drop_incomplete_ohlcv(
            frame,
            "1d",
            calendar_id="XTAIFEX",
        )

    assert result.empty


@pytest.mark.parametrize(
    ("bar_start", "now", "expected_rows"),
    [
        ("2026-01-01 00:00Z", datetime(2026, 1, 31, 12, 0, tzinfo=UTC), 0),
        ("2026-02-01 00:00Z", datetime(2026, 3, 31, 23, 59, tzinfo=UTC), 0),
        ("2026-02-01 00:00Z", datetime(2026, 4, 1, 0, 0, tzinfo=UTC), 1),
        ("2024-01-01 00:00Z", datetime(2024, 2, 29, 23, 59, tzinfo=UTC), 0),
        ("2024-01-01 00:00Z", datetime(2024, 3, 1, 0, 0, tzinfo=UTC), 1),
        ("2025-11-01 00:00Z", datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 1),
    ],
)
def test_drop_incomplete_monthly_uses_real_utc_month_boundary(
    bar_start: str,
    now: datetime,
    expected_rows: int,
) -> None:
    """Without a later bar the newest is held until the next month ends too."""
    frame = pd.DataFrame({"ts": [pd.Timestamp(bar_start)]})

    with patch("librae.brokers.base.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = now
        result = drop_incomplete_ohlcv(frame, "1M")

    assert len(result) == expected_rows


def _taifex_bars_kept(starts: list[str], now: str, timeframe: str) -> list[str]:
    """Run ``starts`` (Taipei bar starts) through XTAIFEX completion at Taipei ``now``."""
    frame = pd.DataFrame(
        {"ts": [pd.Timestamp(start, tz="Asia/Taipei").tz_convert("UTC") for start in starts]}
    )
    with patch("librae.brokers.base.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = (
            pd.Timestamp(now, tz="Asia/Taipei").tz_convert("UTC").to_pydatetime()
        )
        kept = drop_incomplete_ohlcv(frame, timeframe, calendar_id="XTAIFEX")
    return [ts.tz_convert("Asia/Taipei").strftime("%m-%d %H:%M") for ts in kept["ts"]]


@pytest.mark.parametrize(
    ("starts", "now", "timeframe", "kept"),
    [
        pytest.param(
            ["2026-11-05 11:32", "2026-11-05 11:33"],
            "2026-11-05 11:33:30",
            "1m",
            ["11-05 11:32"],
            id="forming minute",
        ),
        pytest.param(
            ["2026-11-05 11:32", "2026-11-05 11:33"],
            "2026-11-05 11:34:00.5",
            "1m",
            ["11-05 11:32"],
            id="closed here, but the source has no later minute yet",
        ),
        pytest.param(
            ["2026-11-05 11:33", "2026-11-05 11:34"],
            "2026-11-05 11:34:00.5",
            "1m",
            ["11-05 11:33"],
            id="a later minute shows the one before it closed",
        ),
        pytest.param(
            ["2026-11-05 11:32", "2026-11-05 11:33"],
            "2026-11-05 11:35:00",
            "1m",
            ["11-05 11:32", "11-05 11:33"],
            id="no minute for a whole period after it",
        ),
        pytest.param(
            ["2026-11-05 13:43", "2026-11-05 13:44"],
            "2026-11-05 13:45:30",
            "1m",
            ["11-05 13:43"],
            id="the session's last minute within the settle",
        ),
        pytest.param(
            ["2026-11-05 13:43", "2026-11-05 13:44"],
            "2026-11-05 13:46:00",
            "1m",
            ["11-05 13:43", "11-05 13:44"],
            id="the session's last minute after the settle",
        ),
        pytest.param(
            ["2026-11-05 11:45", "2026-11-05 12:45"],
            "2026-11-05 13:46:00",
            "1h",
            ["11-05 11:45", "11-05 12:45"],
            id="the session's last hour does not wait for the night session",
        ),
        pytest.param(
            ["2026-11-03 15:00", "2026-11-04 15:00"],
            "2026-11-05 13:45:30",
            "1d",
            ["11-03 15:00"],
            id="a daily bar within the settle",
        ),
        pytest.param(
            ["2026-11-03 15:00", "2026-11-04 15:00"],
            "2026-11-05 13:46:00",
            "1d",
            ["11-03 15:00", "11-04 15:00"],
            id="a daily bar after the settle",
        ),
    ],
)
def test_drop_incomplete_holds_a_calendar_bar_until_the_source_shows_it_closed(
    starts: list[str], now: str, timeframe: str, kept: list[str]
) -> None:
    """Shioaji minutes came back short of their final volume just after their
    close, until the next minute appeared; a bar ending its session has no
    next one to wait for, so it waits only SEGMENT_END_SETTLE."""
    assert _taifex_bars_kept(starts, now, timeframe) == kept


def test_a_bar_is_available_when_drop_incomplete_would_release_it() -> None:
    """The settle a poll waits out is the bar's availability in every mode."""
    minutes = pd.DataFrame(
        {"ts": pd.date_range("2026-11-05 13:43", periods=2, freq="min", tz="Asia/Taipei")}
    )
    stamped = with_settled_available_at(minutes, "1m", "XTAIFEX")
    closes = minutes["ts"] + pd.Timedelta(minutes=1)

    assert stamped["available_at"].tolist() == [closes[0], closes[1] + SEGMENT_END_SETTLE]
    assert settled_available_at(minutes["ts"][1], "M1", "XTAIFEX") == (
        closes[1] + SEGMENT_END_SETTLE
    )
    assert _taifex_bars_kept(
        ["2026-11-05 13:43", "2026-11-05 13:44"], "2026-11-05 13:45:59", "1m"
    ) == ["11-05 13:43"]


def test_the_adapters_declare_the_availability_their_fetches_stamp() -> None:
    shioaji = ShioajiAdapter.__new__(ShioajiAdapter)
    ibkr = IBKRAdapter.__new__(IBKRAdapter)
    taifex_last = pd.Timestamp("2026-11-05 13:44", tz="Asia/Taipei")
    xnys_last = pd.Timestamp("2026-01-05 20:30Z")
    xnys_day = pd.Timestamp("2026-01-05 14:30Z")

    assert shioaji.market_data_available_at(
        taifex_last, "M1", calendar_id="XTAIFEX", session_mode="extended"
    ) == pd.Timestamp("2026-11-05 13:46", tz="Asia/Taipei")
    assert ibkr.market_data_available_at(
        xnys_last, "H1", calendar_id="XNYS", session_mode="regular"
    ) == pd.Timestamp("2026-01-05 21:01Z")
    assert ibkr.market_data_available_at(
        xnys_last, "H1", calendar_id="XNYS", session_mode="extended"
    ) == pd.Timestamp("2026-01-05 21:30Z")
    assert ibkr.market_data_available_at(
        xnys_day, "D1", calendar_id="XNYS", session_mode="regular"
    ) == pd.Timestamp("2026-01-05 21:00Z")
    assert ibkr.market_data_available_at(
        xnys_day, "D1", calendar_id="XNYS", session_mode="extended"
    ) == pd.Timestamp("2026-01-06 14:30Z")


@pytest.mark.parametrize(
    ("now", "expected_rows"),
    [
        (datetime(2026, 1, 30, 20, 59, tzinfo=UTC), 0),
        (datetime(2026, 1, 30, 21, 1, tzinfo=UTC), 1),
    ],
)
def test_drop_incomplete_monthly_uses_last_exchange_session_close(
    now: datetime,
    expected_rows: int,
) -> None:
    frame = pd.DataFrame({"ts": [pd.Timestamp("2026-01-02 14:30Z")]})

    with patch("librae.brokers.base.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = now
        result = drop_incomplete_ohlcv(frame, "MN1", calendar_id="XNYS")

    assert len(result) == expected_rows


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"side": "hold"}, "side"),
        ({"order_type": "stop"}, "order_type"),
        ({"time_in_force": "gtd"}, "time_in_force"),
        ({"quantity": 0}, "quantity"),
        ({"order_type": "limit"}, "limit price"),
        ({"position_effect": "exit"}, "position_effect"),
        ({"position_effect": "closed"}, "position_effect"),
    ],
)
def test_validate_order_signal_rejects_ambiguous_orders(overrides, match):
    signal = {
        "symbol": "TEST",
        "side": "buy",
        "quantity": 1.0,
        "order_type": "market",
        "time_in_force": "ioc",
        "position_effect": "open",
    }
    signal.update(overrides)

    with pytest.raises(ValueError, match=match):
        validate_order_signal(signal)


def test_validate_order_signal_requires_position_effect():
    """Adapters derive reduce-only from it, so omitting it must not pass."""
    with pytest.raises(ValueError, match="position_effect"):
        validate_order_signal(
            {
                "symbol": "TEST",
                "side": "sell",
                "quantity": 1.0,
                "order_type": "market",
                "time_in_force": "ioc",
            }
        )


def test_validate_order_signal_accepts_explicit_market_order():
    validate_order_signal(
        {
            "symbol": "TEST",
            "side": "sell",
            "quantity": 1.0,
            "order_type": "market",
            "time_in_force": "ioc",
            "position_effect": "open",
        }
    )
