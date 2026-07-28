"""Tests for Shioaji's vendor-specific timestamp correction."""

from __future__ import annotations

import pandas as pd

from brokers.shioaji_time import shioaji_ts_ns_to_epoch


def test_corrects_fake_utc_taipei_wall_clock() -> None:
    raw_ns = int(pd.Timestamp("2026-04-01 09:00", tz="UTC").timestamp()) * 1_000_000_000
    expected = int(pd.Timestamp("2026-04-01 09:00", tz="Asia/Taipei").tz_convert("UTC").timestamp())

    assert shioaji_ts_ns_to_epoch(raw_ns) == expected
