"""Tests for strategies.module.data.hashrate — btc_hashrate/btc_difficulty
factor registration and the DB-cached history wrappers."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from strategies.module.data.factors import FREQUENCY_IRREGULAR, _FACTOR_FETCHERS
from strategies.module.data.hashrate import (
    _BTC_SYMBOL,
    attach_hashrate_features,
    fetch_difficulty_history,
    fetch_hashrate_history,
)


def test_btc_hashrate_registered_with_mempool_source():
    assert "btc_hashrate" in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS["btc_hashrate"]
    assert source == "mempool.space"
    assert instrument_type == "spot"
    assert freq == "D1"


def test_btc_difficulty_registered_as_irregular():
    assert "btc_difficulty" in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS["btc_difficulty"]
    assert source == "mempool.space"
    assert freq == FREQUENCY_IRREGULAR


def test_fetch_hashrate_history_renames_value_column():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [1e20]})
    with patch("strategies.module.data.hashrate.get_factor", return_value=fake) as mock_get:
        result = fetch_hashrate_history("2024-01-01", "2024-01-02")

    mock_get.assert_called_once_with(_BTC_SYMBOL, "btc_hashrate", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "btc_hashrate"]


def test_fetch_difficulty_history_renames_value_column():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [5e13]})
    with patch("strategies.module.data.hashrate.get_factor", return_value=fake) as mock_get:
        result = fetch_difficulty_history("2024-01-01", "2024-01-02")

    mock_get.assert_called_once_with(_BTC_SYMBOL, "btc_difficulty", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "btc_difficulty"]


def test_attach_hashrate_features_zero_fills_when_empty():
    ohlcv = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"])})
    with patch("strategies.module.data.hashrate.fetch_hashrate_history", return_value=pd.DataFrame(columns=["timestamp", "btc_hashrate"])), \
         patch("strategies.module.data.hashrate.fetch_difficulty_history", return_value=pd.DataFrame(columns=["timestamp", "btc_difficulty"])):
        result = attach_hashrate_features(ohlcv, "2024-01-01", "2024-01-02")

    assert (result["btc_hashrate"] == 0.0).all()
    assert (result["btc_hashrate_chg_30d"] == 0.0).all()
    assert (result["btc_difficulty"] == 0.0).all()


def test_attach_hashrate_features_computes_30d_change():
    ohlcv = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-31T00:00:00Z"])})
    dates = pd.date_range("2024-01-01", periods=31, freq="D", tz="UTC")
    values = [100.0] * 30 + [150.0]
    hashrate = pd.DataFrame({"timestamp": dates, "btc_hashrate": values})
    with patch("strategies.module.data.hashrate.fetch_hashrate_history", return_value=hashrate), \
         patch("strategies.module.data.hashrate.fetch_difficulty_history", return_value=pd.DataFrame(columns=["timestamp", "btc_difficulty"])):
        result = attach_hashrate_features(ohlcv, "2024-01-01", "2024-01-31")

    assert result["btc_hashrate"].iloc[0] == 150.0
    assert result["btc_hashrate_chg_30d"].iloc[0] == pytest.approx(50.0)
