"""Tests for CryptoAdapter and MarketHub (Sprint 6).

All tests use mocks — no real API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from brokers.crypto_adapter import CryptoAdapter
from brokers.market_hub import MarketHub


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

    # Correct columns
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]

    # Correct shape
    assert len(df) == 3

    # ts is datetime with UTC timezone
    assert pd.api.types.is_datetime64_any_dtype(df["ts"])
    assert df["ts"].dt.tz is not None  # UTC aware
    assert str(df["ts"].dt.tz) == "UTC"

    # Numeric columns
    for col in ["open", "high", "low", "close", "volume"]:
        assert pd.api.types.is_numeric_dtype(df[col])

    # Exchange was called with correct args
    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with("BTC/USDT", timeframe="1h", limit=3)


# ---------------------------------------------------------------------------
# Test 2: symbol passthrough (CCXT uses slash format natively)
# ---------------------------------------------------------------------------

def test_symbol_passthrough(readonly_adapter, mock_ccxt_exchange):
    """CryptoAdapter passes symbol directly to CCXT (BTC/USDT format)."""
    readonly_adapter.fetch_ohlcv("BTC/USDT", "4h", limit=10)
    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with("BTC/USDT", timeframe="4h", limit=10)

    mock_ccxt_exchange.fetch_ohlcv.reset_mock()
    readonly_adapter.fetch_ohlcv("ETH/USDT", "1d", limit=50)
    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with("ETH/USDT", timeframe="1d", limit=50)


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
# Test 4: MarketHub.fetch_ohlcv dispatches to correct adapter
# ---------------------------------------------------------------------------

def test_market_hub_fetch_ohlcv_dispatch():
    mock_adapter = MagicMock()
    expected_df = pd.DataFrame({"ts": [1], "close": [100]})
    mock_adapter.fetch_ohlcv.return_value = expected_df

    hub = MarketHub(adapters={"CRYPTO": mock_adapter})
    result = hub.fetch_ohlcv("CRYPTO", "BTC/USDT", "1h", limit=100)

    mock_adapter.fetch_ohlcv.assert_called_once_with("BTC/USDT", "1h", limit=100)
    pd.testing.assert_frame_equal(result, expected_df)


def test_market_hub_fetch_ohlcv_case_insensitive():
    """Market key lookup is case-insensitive."""
    mock_adapter = MagicMock()
    mock_adapter.fetch_ohlcv.return_value = pd.DataFrame()
    hub = MarketHub(adapters={"CRYPTO": mock_adapter})

    hub.fetch_ohlcv("crypto", "ETH/USDT", "4h")
    mock_adapter.fetch_ohlcv.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5: MarketHub.execute_signal reads signal['market'] and dispatches
# ---------------------------------------------------------------------------

def test_market_hub_execute_signal_dispatch():
    mock_adapter = MagicMock()
    mock_adapter.place_order.return_value = {"id": "123", "status": "filled"}

    hub = MarketHub(adapters={"CRYPTO": mock_adapter})
    signal = {
        "market": "CRYPTO",
        "symbol": "BTC/USDT",
        "side": "buy",
        "quantity": 0.01,
        "order_type": "market",
    }
    result = hub.execute_signal(signal)

    mock_adapter.place_order.assert_called_once_with(signal)
    assert result["status"] == "filled"


def test_market_hub_execute_signal_missing_market():
    hub = MarketHub(adapters={"CRYPTO": MagicMock()})
    with pytest.raises(ValueError, match="market"):
        hub.execute_signal({"symbol": "BTC/USDT"})


# ---------------------------------------------------------------------------
# Test 6: unknown market raises KeyError
# ---------------------------------------------------------------------------

def test_market_hub_unknown_market_raises():
    hub = MarketHub(adapters={"CRYPTO": MagicMock()})

    with pytest.raises(KeyError, match="FOREX"):
        hub.fetch_ohlcv("FOREX", "EUR/USD", "1h")

    with pytest.raises(KeyError, match="STOCK"):
        hub.get_position("STOCK", "AAPL")


# ---------------------------------------------------------------------------
# Bonus: authed adapter can call place_order
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
