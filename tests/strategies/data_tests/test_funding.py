"""Tests for strategies.module.data.funding — funding_rate/basis_premium factor
registration and the DB-cached fetch_funding_rate_history() wrapper."""
from __future__ import annotations

import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.funding import attach_funding_features, fetch_funding_rate_history


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


def test_fetch_premium_day_404_returns_empty_other_http_errors_raise():
    """Regression test: only a 404 (day not published yet) is an expected
    gap. A broader `except Exception` here would silently treat network
    failures/bugs as "no data for this day" instead of surfacing them --
    this exact narrowing was accidentally reverted once already."""
    from strategies.module.data.funding import _fetch_premium_day

    with patch("strategies.module.data.funding.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError("url", 404, "not found", {}, None)
        result = _fetch_premium_day("BTCUSDT", pd.Timestamp("2026-07-20"))
    assert result.empty

    with patch("strategies.module.data.funding.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError("url", 500, "server error", {}, None)
        with pytest.raises(urllib.error.HTTPError):
            _fetch_premium_day("BTCUSDT", pd.Timestamp("2026-07-20"))


def test_fetch_basis_premium_end_before_start_returns_empty():
    from strategies.module.data.funding import _fetch_basis_premium

    today = datetime.now(timezone.utc)
    result = _fetch_basis_premium("BTCUSDT", today, today)
    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


def test_funding_z_3d_is_invariant_to_caller_base_tf():
    """Regression test for the window-semantics bug fixed in
    attach_funding_features: funding_z_3d/funding_cum_3 must be computed on
    the funding print series' own native 8h cadence, not on whatever base_tf
    the caller's OHLCV frame happens to be in. Before the fix, the rolling
    window was computed *after* forward-filling funding_rate onto the
    caller's frame -- so window=9 silently meant "9 hours" at a 1H base_tf
    but "9 days" at a 1D base_tf, instead of the intended ~3 days (9 prints)
    regardless of caller timeframe. Calling attach_funding_features with a
    1H-spaced frame and a 4H-spaced frame over the same window must produce
    matching funding_z_3d values at their shared timestamps."""
    start = pd.Timestamp("2024-01-01", tz="UTC")
    funding_prints = pd.DataFrame({
        "timestamp": pd.date_range(start, periods=90, freq="8h", tz="UTC"),
        "value": np.sin(np.arange(90) / 5.0) * 0.001,  # smooth, non-degenerate signal
    })

    def build(freq: str) -> pd.DataFrame:
        ohlcv = pd.DataFrame({"timestamp": pd.date_range(start, periods=90 * 8 // {"1h": 1, "4h": 4}[freq], freq=freq, tz="UTC")})
        ohlcv = ohlcv[ohlcv["timestamp"] <= funding_prints["timestamp"].max()]
        with patch("strategies.module.data.funding.get_factor", return_value=funding_prints.copy()):
            return attach_funding_features(ohlcv, "BTC/USDT:USDT", "2024-01-01", "2024-01-31")

    result_1h = build("1h").set_index("timestamp")
    result_4h = build("4h").set_index("timestamp")

    shared_ts = result_1h.index.intersection(result_4h.index)
    assert len(shared_ts) > 20  # sanity: the two frames actually overlap enough to compare
    pd.testing.assert_series_equal(
        result_1h.loc[shared_ts, "funding_z_3d"], result_4h.loc[shared_ts, "funding_z_3d"], check_names=False,
    )


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
