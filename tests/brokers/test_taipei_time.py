"""Tests for brokers.taipei_time — TAIFEX calendar/session math.

Pure functions, no Shioaji API involved.
"""

from __future__ import annotations

import pandas as pd

from brokers.taipei_time import (
    floor_to_session_start,
    floor_to_trading_day,
    resample_taifex_ohlcv,
    shioaji_ts_ns_to_epoch,
)


def _utc(iso: str) -> int:
    """True UTC epoch (seconds) for an ISO string interpreted as UTC."""
    return int(pd.Timestamp(iso, tz="UTC").timestamp())


def _taipei(iso: str) -> int:
    """True UTC epoch (seconds) for an ISO string interpreted as Taipei local time."""
    return int(pd.Timestamp(iso, tz="Asia/Taipei").tz_convert("UTC").timestamp())


class TestShioajiTsNsToEpoch:
    def test_corrects_8h_offset(self):
        # Raw ts encodes "2026-04-01 09:00 Taipei" as if it were a UTC epoch.
        raw_ns = _utc("2026-04-01 09:00") * 1_000_000_000
        assert shioaji_ts_ns_to_epoch(raw_ns) == _taipei("2026-04-01 09:00")


class TestFloorToSessionStart:
    def test_day_session_anchors_to_0845_not_hour_grid(self):
        ts = _taipei("2026-04-01 09:30")
        assert floor_to_session_start(ts, target_seconds=3600) == _taipei("2026-04-01 08:45")

    def test_night_session_restarts_at_1500_not_prior_grid(self):
        ts = _taipei("2026-04-01 15:30")
        assert floor_to_session_start(ts, target_seconds=3600) == _taipei("2026-04-01 15:00")

    def test_post_midnight_belongs_to_previous_nights_open(self):
        # Whole-session-width bucket: any instant within the night session
        # (15:00 -> next day 05:00, 14h) collapses to the session's own
        # open, regardless of which calendar day the instant's clock reads.
        ts = _taipei("2026-04-02 02:30")
        assert floor_to_session_start(ts, target_seconds=14 * 3600) == _taipei("2026-04-01 15:00")


class TestFloorToTradingDay:
    def test_day_session_same_calendar_day(self):
        assert floor_to_trading_day(_taipei("2026-04-01 09:30")) == _taipei("2026-04-01 00:00")

    def test_night_session_rolls_to_next_day(self):
        assert floor_to_trading_day(_taipei("2026-04-01 15:30")) == _taipei("2026-04-02 00:00")

    def test_post_midnight_night_session_still_next_day(self):
        assert floor_to_trading_day(_taipei("2026-04-02 02:30")) == _taipei("2026-04-02 00:00")


class TestResampleTaifexOhlcv:
    def test_1h_bars_align_to_session_open(self):
        # All three 1-min bars fall within the first hourly bucket after the
        # session opens (08:45-09:45); a plain round-hour grid would instead
        # split 08:45-09:00 into the previous (pre-market) hour.
        idx = pd.DatetimeIndex(
            ["2026-04-01 08:45", "2026-04-01 09:00", "2026-04-01 09:30"],
            tz="Asia/Taipei",
        ).tz_convert("UTC")
        df = pd.DataFrame(
            {
                "open": [100.0, 101.0, 102.0],
                "high": [105.0, 106.0, 107.0],
                "low": [99.0, 100.0, 101.0],
                "close": [104.0, 105.0, 106.0],
                "volume": [10.0, 20.0, 30.0],
            },
            index=idx,
        )
        df.index.name = "ts"

        out = resample_taifex_ohlcv(df, target_seconds=3600)

        assert len(out) == 1
        assert out.index[0] == pd.Timestamp("2026-04-01 08:45", tz="Asia/Taipei").tz_convert("UTC")
        assert out["open"].iloc[0] == 100.0
        assert out["high"].iloc[0] == 107.0
        assert out["low"].iloc[0] == 99.0
        assert out["close"].iloc[0] == 106.0
        assert out["volume"].iloc[0] == 60.0
