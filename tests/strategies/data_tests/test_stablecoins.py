"""Tests for strategies.module.data.stablecoins — stablecoin_mcap_total
factor registration, the DB-cached history wrapper, and attach_* merge."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.stablecoins import (
    _MARKET_WIDE_SYMBOL,
    attach_stablecoin_features,
    fetch_stablecoin_mcap_history,
)


def test_stablecoin_mcap_registered_with_defillama_source():
    assert "stablecoin_mcap_total" in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS["stablecoin_mcap_total"]
    assert source == "defillama"
    assert instrument_type == "spot"
    assert freq == "D1"


def test_fetch_stablecoin_mcap_history_renames_value_column():
    fake = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]),
        "value": [1.5e11],
    })
    with patch("strategies.module.data.stablecoins.get_factor", return_value=fake) as mock_get:
        result = fetch_stablecoin_mcap_history("2024-01-01", "2024-01-02")

    mock_get.assert_called_once_with(_MARKET_WIDE_SYMBOL, "stablecoin_mcap_total", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "stablecoin_mcap_total"]


def test_fetch_end_before_start_returns_empty():
    from strategies.module.data.stablecoins import _fetch_stablecoin_mcap

    with patch(
        "strategies.module.data.stablecoins.defillama_client.fetch_stablecoin_mcap",
        return_value=pd.DataFrame({"date": pd.to_datetime(["2024-06-01"], utc=True), "value": [1.0]}),
    ):
        today = datetime.now(timezone.utc)
        result = _fetch_stablecoin_mcap("CRYPTO_MARKET", today, today - pd.Timedelta(days=1))
    assert result.empty


def test_attach_stablecoin_features_zero_fills_when_empty():
    ohlcv = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"])})
    with patch("strategies.module.data.stablecoins.fetch_stablecoin_mcap_history", return_value=pd.DataFrame(columns=["timestamp", "stablecoin_mcap_total"])):
        result = attach_stablecoin_features(ohlcv, "2024-01-01", "2024-01-02")

    assert (result["stablecoin_mcap_total"] == 0.0).all()
    assert (result["stablecoin_mcap_chg_7d"] == 0.0).all()


def test_attach_stablecoin_features_computes_7d_change():
    ohlcv = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-09T00:00:00Z"])})
    dates = pd.date_range("2024-01-01", periods=9, freq="D", tz="UTC")
    mcap = pd.DataFrame({
        "timestamp": dates,
        "stablecoin_mcap_total": [100.0, 100, 100, 100, 100, 100, 100, 100, 110.0],
    })
    with patch("strategies.module.data.stablecoins.fetch_stablecoin_mcap_history", return_value=mcap):
        result = attach_stablecoin_features(ohlcv, "2024-01-01", "2024-01-09")

    assert result["stablecoin_mcap_total"].iloc[0] == 110.0
    assert result["stablecoin_mcap_chg_7d"].iloc[0] == pytest.approx(10.0)
