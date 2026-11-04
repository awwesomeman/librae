"""Write-time bar timestamp checks, against the misstamps real feeds produced.

Each rejected shape here is one a provider actually delivered: bars stamped at
their close, and local time stored as UTC. Each accepted shape is one a naive
in-session check would wrongly refuse.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from librae.core.market_data import (
    BarTimestampError,
    MarketDataSubscription,
    validate_bar_starts,
)
from librae.db.timescale_writer import write_run_bars


def _taipei(*values: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([pd.Timestamp(v, tz="Asia/Taipei") for v in values]).tz_convert("UTC")


def _minutes(start: str, end: str, tz: str = "Asia/Taipei") -> pd.DatetimeIndex:
    """Minute starts in [start, end), local wall clock."""
    return pd.date_range(start, end, freq="1min", inclusive="left", tz=tz).tz_convert("UTC")


def _check(
    timestamps: pd.DatetimeIndex,
    timeframe: str,
    calendar_id: str,
    session_mode: str = "extended",
) -> None:
    validate_bar_starts(timestamps, timeframe, calendar_id, session_mode=session_mode)


# --- rejected: the shapes that reached the database before -----------------


def test_close_stamped_taifex_hour_bars_leave_a_rejected_stub_at_the_close() -> None:
    # Minute bars stamped at their close shift every bucket one minute late;
    # resampled into H1, the day segment's last minute becomes a 13:45 bar.
    with pytest.raises(BarTimestampError) as caught:
        _check(_taipei("2026-11-02 12:45", "2026-11-02 13:45"), "H1", "XTAIFEX")

    message = str(caught.value)
    assert "1 of 2 timestamps" in message
    assert "2026-11-02T05:45:00+00:00 (outside the XTAIFEX trading sessions)" in message


def test_close_stamped_taifex_minutes_are_rejected_at_the_segment_close() -> None:
    late = _minutes("2026-11-02 08:46", "2026-11-02 13:46")

    with pytest.raises(BarTimestampError, match=r"1 of 300 timestamps.*05:45:00"):
        _check(late, "M1", "XTAIFEX")


def test_taipei_minutes_stored_as_utc_are_rejected_with_every_bad_row_counted() -> None:
    # A close-stamped night session (15:01-05:00) read as UTC lands 8 hours
    # late, 23:01-13:00 Taipei. Most of it still falls in some session; the
    # 225 minutes in the 05:00-08:45 closed gap are what gives it away.
    night = _minutes("2026-11-02 15:00", "2026-11-03 05:00")
    stamps = night + pd.Timedelta(hours=8, minutes=1)

    with pytest.raises(BarTimestampError) as caught:
        _check(stamps, "M1", "XTAIFEX")

    message = str(caught.value)
    assert "225 of 840 timestamps" in message
    assert "first 5: 2026-11-02T21:00:00+00:00 (outside the XTAIFEX trading sessions)" in message
    assert message.count("(outside the XTAIFEX trading sessions)") == 5


def test_misaligned_always_open_bars_are_rejected() -> None:
    stamps = pd.DatetimeIndex(["2024-06-01 00:00", "2024-06-01 01:30"], tz="UTC")

    with pytest.raises(BarTimestampError, match="its H1 bar starts at 2024-06-01T01:00:00"):
        _check(stamps, "H1", "24/7")


def test_daily_taifex_bar_at_taipei_midnight_is_rejected() -> None:
    # A TAIFEX D1 bar starts at the night open before its session label.
    with pytest.raises(BarTimestampError, match="its D1 bar starts at 2026-11-02T07:00:00"):
        _check(_taipei("2026-11-03 00:00"), "D1", "XTAIFEX")


# --- accepted: shapes a naive in-session check would refuse ---------------


def test_index_closing_auction_minutes_pass_in_extended_mode() -> None:
    # IX0001 publishes its official close from the 13:30-13:33 auction, after
    # XTAI's regular session the calendar knows about.
    _check(
        _minutes("2026-11-03 09:00", "2026-11-03 13:34"),
        "M1",
        "XTAI",
    )


def test_closing_auction_minutes_are_rejected_as_regular_session_bars() -> None:
    with pytest.raises(BarTimestampError, match="4 of 4"):
        _check(_minutes("2026-11-03 13:30", "2026-11-03 13:34"), "M1", "XTAI", "regular")


def test_extended_bars_off_a_trading_day_or_grid_are_rejected() -> None:
    saturday = _taipei("2026-10-31 10:00")
    off_grid = _taipei("2026-11-03 10:00:30")

    with pytest.raises(BarTimestampError) as caught:
        _check(saturday.append(off_grid), "M1", "XTAI")

    assert "2026-10-31 is not a XTAI trading day" in str(caught.value)
    assert "not on the M1 grid of Asia/Taipei wall-clock time" in str(caught.value)


def test_extended_grid_is_wall_clock_across_a_dst_change() -> None:
    # 2024-03-11 is the first New York session after the spring-forward.
    _check(
        pd.DatetimeIndex(
            [
                pd.Timestamp("2024-03-08 04:00", tz="America/New_York"),
                pd.Timestamp("2024-03-11 04:00", tz="America/New_York"),
            ]
        ),
        "H1",
        "XNYS",
    )


def test_taifex_night_session_crosses_midnight_and_ends_on_saturday() -> None:
    # Friday's night session runs to Saturday 05:00 and belongs to Monday.
    _check(_minutes("2026-10-30 15:00", "2026-10-31 05:00"), "M1", "XTAIFEX")
    _check(_taipei("2026-10-30 15:00", "2026-10-31 00:00", "2026-10-31 04:00"), "H1", "XTAIFEX")

    with pytest.raises(BarTimestampError, match="1 of 1"):
        _check(_taipei("2026-10-31 05:00"), "M1", "XTAIFEX")


def test_always_open_calendar_accepts_weekends() -> None:
    _check(pd.date_range("2024-06-01", periods=48, freq="1h", tz="UTC"), "H1", "24/7")


def test_daily_bars_at_their_session_start_pass() -> None:
    _check(_taipei("2026-10-30 15:00"), "D1", "XTAIFEX")
    _check(pd.DatetimeIndex(["2024-06-03 13:30"], tz="UTC"), "D1", "XNYS", "regular")


def test_repeated_timestamps_are_checked_once() -> None:
    # A chain table holds one row per contract at each bar.
    stamps = _taipei("2026-11-03 09:00").repeat(3)

    _check(stamps, "M1", "XTAIFEX")
    with pytest.raises(BarTimestampError, match="1 of 1"):
        _check(_taipei("2026-11-03 05:30").repeat(3), "M1", "XTAIFEX")


# --- the writer ------------------------------------------------------------


def test_write_run_bars_refuses_the_whole_batch_before_touching_the_database() -> None:
    index = _taipei("2026-11-02 12:45", "2026-11-02 13:45")
    frame = pd.DataFrame(
        {
            "open": np.full(2, 100.0),
            "high": np.full(2, 101.0),
            "low": np.full(2, 99.0),
            "close": np.full(2, 100.0),
            "volume": np.full(2, 1.0),
        },
        index=pd.DatetimeIndex(index, name="ts"),
    )
    subscription = MarketDataSubscription(
        symbol="TXFR1",
        timeframe="H1",
        calendar_id="XTAIFEX",
        session_mode="extended",
        data_source="shioaji",
        instrument_type="contract_monthly",
    )

    with (
        patch("librae.db.timescale_writer.get_conn") as get_conn,
        pytest.raises(BarTimestampError, match="run bars write for 'TXFR1' 'shioaji'"),
    ):
        write_run_bars("run-1", subscription, frame)

    get_conn.assert_not_called()
