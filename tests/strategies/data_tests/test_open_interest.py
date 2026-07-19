"""Tests for strategies.data.open_interest — open_interest factor
registration and the DB-cached fetch_open_interest_history() wrapper."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from strategies.data.factors import _FACTOR_FETCHERS
from strategies.data.open_interest import _fetch_oi_range, fetch_open_interest_history


def test_open_interest_registered_with_archive_source():
    assert "open_interest" in _FACTOR_FETCHERS
    _fn, source = _FACTOR_FETCHERS["open_interest"]
    assert source == "data.binance.vision"


def test_fetch_open_interest_history_renames_value_column():
    fake = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]),
        "value": [12345.0],
    })
    with patch("strategies.data.open_interest.get_factor", return_value=fake) as mock_get:
        result = fetch_open_interest_history("BTCUSDT", "2024-01-01", "2024-01-02")

    mock_get.assert_called_once_with("BTCUSDT", "open_interest", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "open_interest"]


def test_fetch_oi_range_end_before_start_returns_empty():
    """end_dt normalized-to-yesterday can land before start_dt for a
    start_dt of 'today' — must return empty, not fetch a negative range."""
    today = datetime.now(timezone.utc)
    result = _fetch_oi_range("BTCUSDT", today, today)
    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


def test_fetch_oi_range_forward_fills_zero_readings():
    fake_day = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T00:05:00Z", "2024-01-01T00:10:00Z"]),
        "open_interest": [100.0, 0.0, 105.0],
    })
    with patch("strategies.data.open_interest._fetch_day", return_value=fake_day):
        result = _fetch_oi_range(
            "BTCUSDT",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 23, tzinfo=timezone.utc),
        )
    assert list(result["value"]) == [100.0, 100.0, 105.0]
