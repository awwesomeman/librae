"""Tests for strategies.module.data.funding — funding_rate factor registration and
the DB-cached fetch_funding_rate_history() wrapper."""
from __future__ import annotations

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
