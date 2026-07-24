"""Tests for strategies.module.data.open_interest — open_interest/positioning
factor registration and the DB-cached fetch_open_interest_history() wrapper."""
from __future__ import annotations

import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.open_interest import (
    _COLUMNS,
    _metrics_column_fetcher,
    attach_oi_features,
    fetch_open_interest_history,
)

_OI_FETCHER = _metrics_column_fetcher(_COLUMNS["open_interest"])


@pytest.mark.parametrize("factor_name", list(_COLUMNS))
def test_metrics_factors_registered_with_archive_source(factor_name):
    assert factor_name in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS[factor_name]
    assert source == "data.binance.vision"
    assert instrument_type == "contract_perpetual"
    assert freq == "M5"


def test_fetch_open_interest_history_renames_value_column():
    fake = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]),
        "value": [12345.0],
    })
    with patch("strategies.module.data.open_interest.get_factor", return_value=fake) as mock_get:
        result = fetch_open_interest_history("BTCUSDT", "2024-01-01", "2024-01-02")

    mock_get.assert_called_once_with("BTCUSDT", "open_interest", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "open_interest"]


def test_metrics_column_fetcher_end_before_start_returns_empty():
    """end_dt normalized-to-yesterday can land before start_dt for a
    start_dt of 'today' — must return empty, not fetch a negative range."""
    today = datetime.now(timezone.utc)
    result = _OI_FETCHER("BTCUSDT", today, today)
    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


def test_metrics_column_fetcher_forward_fills_zero_readings():
    fake_day = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T00:05:00Z", "2024-01-01T00:10:00Z"]),
        "value": [100.0, 0.0, 105.0],
    })
    with patch("strategies.module.data.open_interest._fetch_day", return_value=fake_day):
        result = _OI_FETCHER(
            "BTCUSDT",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 23, tzinfo=timezone.utc),
        )
    assert list(result["value"]) == [100.0, 100.0, 105.0]


def test_fetch_day_extracts_correct_raw_column():
    """Different factor_names must read their own column, not always OI."""
    from strategies.module.data.open_interest import _fetch_day

    raw = pd.DataFrame({
        "create_time": ["2024-01-01 00:00:00"],
        "sum_open_interest": [100.0],
        "sum_toptrader_long_short_ratio": [1.55],
    })
    with patch("strategies.module.data.open_interest.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.read.return_value = b"fake"
        with patch("strategies.module.data.open_interest.zipfile.ZipFile") as mock_zip:
            mock_zip.return_value.__enter__.return_value.namelist.return_value = ["f.csv"]
            mock_zip.return_value.__enter__.return_value.read.return_value = b"csv bytes"
            with patch("strategies.module.data.open_interest.pd.read_csv", return_value=raw):
                oi_row = _fetch_day("BTCUSDT", pd.Timestamp("2024-01-01"), "sum_open_interest")
                ratio_row = _fetch_day("BTCUSDT", pd.Timestamp("2024-01-01"), "sum_toptrader_long_short_ratio")

    assert oi_row["value"].iloc[0] == 100.0
    assert ratio_row["value"].iloc[0] == 1.55


def test_fetch_day_404_returns_empty_other_http_errors_raise():
    """Regression test: only a 404 (day not published yet) is an expected
    gap. A broader `except Exception` here would silently treat network
    failures/bugs as "no data for this day" instead of surfacing them --
    this exact narrowing was accidentally reverted once already."""
    from strategies.module.data.open_interest import _fetch_day

    with patch("strategies.module.data.open_interest.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError("url", 404, "not found", {}, None)
        result = _fetch_day("BTCUSDT", pd.Timestamp("2024-01-01"), "sum_open_interest")
    assert result.empty

    with patch("strategies.module.data.open_interest.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError("url", 500, "server error", {}, None)
        with pytest.raises(urllib.error.HTTPError):
            _fetch_day("BTCUSDT", pd.Timestamp("2024-01-01"), "sum_open_interest")


def test_open_interest_change_24h_is_invariant_to_caller_base_tf():
    """Regression test for the same window-semantics bug fixed in
    attach_funding_features (RESEARCH_METHODOLOGY.md): open_interest_change_24h
    must be computed on OI's own native 5-min cadence, not on whatever base_tf
    the caller's OHLCV frame happens to be in. A shift(24) on the merged frame
    would silently mean "24 bars of base_tf" (24h at 1H, 96h at 4H) instead of
    the intended 24 real hours regardless of caller timeframe."""
    start = pd.Timestamp("2024-01-01", tz="UTC")
    oi_native = pd.DataFrame({
        "timestamp": pd.date_range(start, periods=2000, freq="5min", tz="UTC"),
        "open_interest": 1000.0 + np.sin(np.arange(2000) / 15.0) * 50.0,
    })

    def build(freq: str) -> pd.DataFrame:
        periods = 2000 * 5 // {"1h": 60, "4h": 240}[freq]
        ohlcv = pd.DataFrame({"timestamp": pd.date_range(start, periods=periods, freq=freq, tz="UTC")})
        ohlcv = ohlcv[ohlcv["timestamp"] <= oi_native["timestamp"].max()]
        with patch("strategies.module.data.open_interest.fetch_open_interest_history", return_value=oi_native.copy()):
            return attach_oi_features(ohlcv, "BTCUSDT", "2024-01-01", "2024-01-31")

    result_1h = build("1h").set_index("timestamp")
    result_4h = build("4h").set_index("timestamp")

    shared_ts = result_1h.index.intersection(result_4h.index)
    assert len(shared_ts) > 10  # sanity: the two frames actually overlap enough to compare
    pd.testing.assert_series_equal(
        result_1h.loc[shared_ts, "open_interest_change_24h"],
        result_4h.loc[shared_ts, "open_interest_change_24h"],
        check_names=False,
    )
