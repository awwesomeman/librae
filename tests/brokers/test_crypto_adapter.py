"""Tests for CryptoAdapter.

All tests use mocks — no real API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from brokers.crypto_adapter import CryptoAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ccxt_exchange():
    """Return a mock CCXT exchange instance."""
    exchange = MagicMock()
    # Simulate fetch_ohlcv returning 3 candles (ts in ms)
    exchange.fetch_ohlcv.return_value = [
        [1_700_000_000_000, 35000.0, 35500.0, 34800.0, 35200.0, 100.5],
        [1_700_003_600_000, 35200.0, 35800.0, 35100.0, 35600.0, 120.3],
        [1_700_007_200_000, 35600.0, 36000.0, 35400.0, 35900.0, 95.7],
    ]
    return exchange


@pytest.fixture
def readonly_adapter(mock_ccxt_exchange):
    """CryptoAdapter in read-only mode (no API key) with mocked exchange."""
    with patch("brokers.crypto_adapter._require_ccxt") as mock_ccxt:
        mock_exchange_cls = MagicMock(return_value=mock_ccxt_exchange)
        mock_ccxt.return_value = MagicMock(**{"binance": mock_exchange_cls})
        # Manually construct to bypass __init__ ccxt lookup
        adapter = CryptoAdapter.__new__(CryptoAdapter)
        adapter._exchange = mock_ccxt_exchange
        adapter._read_only = True
        adapter._exchange_id = "binance"
    return adapter


@pytest.fixture
def authed_adapter(mock_ccxt_exchange):
    """CryptoAdapter with API key (authenticated mode)."""
    adapter = CryptoAdapter.__new__(CryptoAdapter)
    adapter._exchange = mock_ccxt_exchange
    adapter._read_only = False
    adapter._exchange_id = "binance"
    return adapter


# ---------------------------------------------------------------------------
# Test 1: fetch_ohlcv returns correct DataFrame format
# ---------------------------------------------------------------------------

def test_fetch_ohlcv_dataframe_format(readonly_adapter, mock_ccxt_exchange):
    df = readonly_adapter.fetch_ohlcv("BTC/USDT", "1h", limit=3)

    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert len(df) == 3

    assert pd.api.types.is_datetime64_any_dtype(df["ts"])
    assert df["ts"].dt.tz is not None
    assert str(df["ts"].dt.tz) == "UTC"

    for col in ["open", "high", "low", "close", "volume"]:
        assert pd.api.types.is_numeric_dtype(df[col])

    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with("BTC/USDT", timeframe="1h", limit=3, since=None)


# ---------------------------------------------------------------------------
# Test 2: symbol passthrough (CCXT uses slash format natively)
# ---------------------------------------------------------------------------

def test_symbol_passthrough(readonly_adapter, mock_ccxt_exchange):
    readonly_adapter.fetch_ohlcv("BTC/USDT", "4h", limit=10)
    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with("BTC/USDT", timeframe="4h", limit=10, since=None)

    mock_ccxt_exchange.fetch_ohlcv.reset_mock()
    readonly_adapter.fetch_ohlcv("ETH/USDT", "1d", limit=50)
    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with("ETH/USDT", timeframe="1d", limit=50, since=None)


# ---------------------------------------------------------------------------
# Test 3: read-only mode raises NotImplementedError on place_order
# ---------------------------------------------------------------------------

def test_readonly_place_order_raises(readonly_adapter):
    signal = {
        "market": "CRYPTO",
        "symbol": "BTC/USDT",
        "side": "buy",
        "quantity": 0.01,
        "order_type": "market",
    }
    with pytest.raises(NotImplementedError, match="read-only"):
        readonly_adapter.place_order(signal)


def test_readonly_get_position_raises(readonly_adapter):
    with pytest.raises(NotImplementedError, match="read-only"):
        readonly_adapter.get_position("BTC/USDT")


# ---------------------------------------------------------------------------
# Test 4: authed adapter can call place_order
# ---------------------------------------------------------------------------

def test_authed_adapter_place_order(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.create_order.return_value = {"id": "ord_1", "status": "open"}
    signal = {
        "symbol": "BTC/USDT",
        "side": "buy",
        "quantity": 0.01,
        "order_type": "market",
    }
    result = authed_adapter.place_order(signal)
    assert result["id"] == "ord_1"
    mock_ccxt_exchange.create_order.assert_called_once()
