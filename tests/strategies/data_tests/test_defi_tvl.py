"""Tests for strategies.module.data.defi_tvl — defi_tvl_total factor
registration and the DB-cached history wrapper."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.defi_tvl import _MARKET_WIDE_SYMBOL, attach_defi_tvl_features, fetch_defi_tvl_history


def test_defi_tvl_registered_with_defillama_source():
    assert "defi_tvl_total" in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS["defi_tvl_total"]
    assert source == "defillama"
    assert instrument_type == "spot"
    assert freq == "D1"


def test_fetch_defi_tvl_history_renames_value_column():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [8e10]})
    with patch("strategies.module.data.defi_tvl.get_factor", return_value=fake) as mock_get:
        result = fetch_defi_tvl_history("2024-01-01", "2024-01-02")

    mock_get.assert_called_once_with(_MARKET_WIDE_SYMBOL, "defi_tvl_total", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "defi_tvl_total"]


def test_attach_defi_tvl_features_zero_fills_when_empty():
    ohlcv = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"])})
    with patch("strategies.module.data.defi_tvl.fetch_defi_tvl_history", return_value=pd.DataFrame(columns=["timestamp", "defi_tvl_total"])):
        result = attach_defi_tvl_features(ohlcv, "2024-01-01", "2024-01-02")

    assert (result["defi_tvl_total"] == 0.0).all()
