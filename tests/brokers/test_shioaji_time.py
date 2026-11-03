"""Tests for Shioaji's vendor-specific timestamp correction."""

from __future__ import annotations

import pandas as pd
from librae.brokers.shioaji_time import shioaji_kbar_starts


def test_minute_end_in_fake_utc_becomes_the_utc_minute_start() -> None:
    raw_ns = pd.Timestamp("2026-11-02 08:46", tz="UTC").value

    starts = shioaji_kbar_starts([raw_ns])

    assert list(starts) == [pd.Timestamp("2026-11-02 08:45", tz="Asia/Taipei")]
    assert str(starts.tz) == "UTC"
