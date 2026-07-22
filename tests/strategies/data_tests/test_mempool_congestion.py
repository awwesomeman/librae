"""Tests for strategies.module.data.mempool_congestion —
btc_mempool_tx_count factor registration and the DB-cached history wrapper."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.mempool_congestion import (
    _BTC_SYMBOL,
    attach_mempool_congestion_features,
    fetch_mempool_tx_count_history,
)


def test_mempool_tx_count_registered_with_mempool_source():
    assert "btc_mempool_tx_count" in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS["btc_mempool_tx_count"]
    assert source == "mempool.space"
    assert instrument_type == "spot"
    assert freq == "H12"


def test_fetch_mempool_tx_count_history_renames_value_column():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [50000.0]})
    with patch("strategies.module.data.mempool_congestion.get_factor", return_value=fake) as mock_get:
        result = fetch_mempool_tx_count_history("2024-01-01", "2024-01-02")

    mock_get.assert_called_once_with(_BTC_SYMBOL, "btc_mempool_tx_count", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "btc_mempool_tx_count"]


def test_attach_mempool_congestion_features_zero_fills_when_empty():
    ohlcv = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"])})
    with patch("strategies.module.data.mempool_congestion.fetch_mempool_tx_count_history", return_value=pd.DataFrame(columns=["timestamp", "btc_mempool_tx_count"])):
        result = attach_mempool_congestion_features(ohlcv, "2024-01-01", "2024-01-02")

    assert (result["btc_mempool_tx_count"] == 0.0).all()
