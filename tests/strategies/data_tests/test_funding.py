"""Tests for strategies.module.data.funding — funding_rate/basis_premium factor
registration and the DB-cached fetch_funding_rate_history() wrapper."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.funding import fetch_funding_rate_history


def test_funding_rate_registered_with_binanceusdm_source():
    assert "funding_rate" in _FACTOR_FETCHERS
    _fn, source, instrument_type, _freq = _FACTOR_FETCHERS["funding_rate"]
    assert source == "binanceusdm"
    assert instrument_type == "contract_perpetual"


def test_fetch_funding_rate_history_renames_value_column():
    fake = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]),
        "value": [0.0001],
    })
    with patch("strategies.module.data.funding.get_factor", return_value=fake) as mock_get:
        result = fetch_funding_rate_history("BTC/USDT:USDT", "2024-01-01", "2024-01-02")

    mock_get.assert_called_once_with("BTC/USDT:USDT", "funding_rate", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "funding_rate"]


def test_basis_premium_registered_with_archive_source():
    assert "basis_premium" in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS["basis_premium"]
    assert source == "data.binance.vision"
    assert instrument_type == "contract_perpetual"
    assert freq == "H1"


def test_fetch_premium_day_extracts_close_column():
    from strategies.module.data.funding import _fetch_premium_day

    raw = pd.DataFrame({
        "open_time": [1784505600000],
        "open": [-0.00033569], "high": [0.00010948], "low": [-0.00103918],
        "close": [-0.00045862], "volume": [0],
    })
    with patch("strategies.module.data.funding.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.read.return_value = b"fake"
        with patch("strategies.module.data.funding.zipfile.ZipFile") as mock_zip:
            mock_zip.return_value.__enter__.return_value.namelist.return_value = ["f.csv"]
            mock_zip.return_value.__enter__.return_value.read.return_value = b"csv bytes"
            with patch("strategies.module.data.funding.pd.read_csv", return_value=raw):
                result = _fetch_premium_day("BTCUSDT", pd.Timestamp("2026-07-20"))

    assert result["value"].iloc[0] == -0.00045862


def test_fetch_basis_premium_end_before_start_returns_empty():
    from strategies.module.data.funding import _fetch_basis_premium

    today = datetime.now(timezone.utc)
    result = _fetch_basis_premium("BTCUSDT", today, today)
    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


def test_fetch_basis_premium_does_not_zero_fill():
    """Unlike OI/ratio metrics, an exact-0 premium is a legitimate reading —
    must not be forward-filled away."""
    from strategies.module.data.funding import _fetch_basis_premium

    fake_day = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"]),
        "value": [0.0001, 0.0],
    })
    with patch("strategies.module.data.funding._fetch_premium_day", return_value=fake_day):
        result = _fetch_basis_premium(
            "BTCUSDT", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, 23, tzinfo=timezone.utc),
        )
    assert result["value"].tolist() == [0.0001, 0.0]
