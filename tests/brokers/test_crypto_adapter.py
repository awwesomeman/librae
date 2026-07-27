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

    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with(
        "BTC/USDT", timeframe="1h", limit=3, since=None
    )


# ---------------------------------------------------------------------------
# Test 2: symbol passthrough (CCXT uses slash format natively)
# ---------------------------------------------------------------------------


def test_symbol_passthrough(readonly_adapter, mock_ccxt_exchange):
    readonly_adapter.fetch_ohlcv("BTC/USDT", "4h", limit=10)
    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with(
        "BTC/USDT", timeframe="4h", limit=10, since=None
    )

    mock_ccxt_exchange.fetch_ohlcv.reset_mock()
    readonly_adapter.fetch_ohlcv("ETH/USDT", "1d", limit=50)
    mock_ccxt_exchange.fetch_ohlcv.assert_called_once_with(
        "ETH/USDT", timeframe="1d", limit=50, since=None
    )


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


def test_readonly_get_balance_raises(readonly_adapter):
    with pytest.raises(NotImplementedError, match="read-only"):
        readonly_adapter.get_balance("USDT")


# ---------------------------------------------------------------------------
# Test 3b: get_balance
# ---------------------------------------------------------------------------


def test_authed_get_balance_parses_free_used_total(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.fetch_balance.return_value = {
        "USDT": {"free": 900.0, "used": 100.0, "total": 1000.0},
        "info": {},
    }
    balance = authed_adapter.get_balance("USDT")
    assert balance == {"free": 900.0, "used": 100.0, "total": 1000.0}


def test_get_balance_missing_currency_returns_zeros(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.fetch_balance.return_value = {
        "BTC": {"free": 1.0, "used": 0.0, "total": 1.0}
    }
    balance = authed_adapter.get_balance("USDT")
    assert balance == {"free": 0.0, "used": 0.0, "total": 0.0}


def test_spot_position_uses_inventory_balance(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.market.return_value = {
        "symbol": "BTC/USDT",
        "type": "spot",
        "spot": True,
        "base": "BTC",
    }
    mock_ccxt_exchange.fetch_balance.return_value = {
        "BTC": {"free": 0.7, "used": 0.3, "total": 1.0}
    }

    position = authed_adapter.get_position("BTC/USDT")

    assert position == {
        "symbol": "BTC/USDT",
        "size": 1.0,
        "avg_price": None,
        "unrealized_pnl": 0.0,
    }
    mock_ccxt_exchange.fetch_positions.assert_not_called()


def test_derivative_short_position_has_negative_size(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.market.return_value = {
        "symbol": "BTC/USDT:USDT",
        "type": "swap",
        "spot": False,
    }
    mock_ccxt_exchange.fetch_positions.return_value = [
        {
            "symbol": "BTC/USDT:USDT",
            "contracts": 2.0,
            "side": "short",
            "entryPrice": 50_000.0,
        }
    ]

    position = authed_adapter.get_position("BTC/USDT:USDT")

    assert position["size"] == -2.0
    assert position["avg_price"] == 50_000.0


# ---------------------------------------------------------------------------
# Test 4: authed adapter can call place_order
# ---------------------------------------------------------------------------


def test_prepare_order_applies_precision_and_limits(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.market.return_value = {
        "symbol": "BTC/USDT",
        "type": "spot",
        "spot": True,
        "contractSize": 1.0,
        "limits": {
            "amount": {"min": 0.001, "max": 10.0},
            "price": {"min": 1.0, "max": None},
            "cost": {"min": 10.0, "max": None},
        },
    }
    mock_ccxt_exchange.amount_to_precision.return_value = "0.123"
    mock_ccxt_exchange.price_to_precision.return_value = "100.12"

    prepared = authed_adapter.prepare_order(
        {
            "symbol": "BTC/USDT",
            "side": "buy",
            "quantity": 0.12345,
            "order_type": "limit",
            "price": 100.123,
            "position_effect": "open",
        }
    )

    assert prepared["quantity"] == 0.123
    assert prepared["price"] == 100.12


def test_prepare_order_rejects_spot_short_open(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.market.return_value = {
        "symbol": "BTC/USDT",
        "type": "spot",
        "spot": True,
        "limits": {},
    }
    mock_ccxt_exchange.amount_to_precision.return_value = "0.1"

    with pytest.raises(ValueError, match="cannot open a short"):
        authed_adapter.prepare_order(
            {
                "symbol": "BTC/USDT",
                "side": "sell",
                "quantity": 0.1,
                "order_type": "market",
                "position_effect": "open",
                "reference_price": 50_000.0,
            }
        )


def test_prepare_order_rejects_min_notional(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.market.return_value = {
        "symbol": "BTC/USDT",
        "type": "spot",
        "spot": True,
        "limits": {"cost": {"min": 10.0, "max": None}},
    }
    mock_ccxt_exchange.amount_to_precision.return_value = "0.0001"

    with pytest.raises(ValueError, match="below minimum"):
        authed_adapter.prepare_order(
            {
                "symbol": "BTC/USDT",
                "side": "buy",
                "quantity": 0.0001,
                "order_type": "market",
                "position_effect": "open",
                "reference_price": 50_000.0,
            }
        )


def test_prepare_order_requires_derivative_contract_size(
    authed_adapter,
    mock_ccxt_exchange,
):
    mock_ccxt_exchange.market.return_value = {
        "symbol": "BTC/USDT:USDT",
        "type": "swap",
        "contract": True,
        "spot": False,
        "limits": {},
    }
    mock_ccxt_exchange.amount_to_precision.return_value = "1"

    with pytest.raises(ValueError, match="contractSize"):
        authed_adapter.prepare_order(
            {
                "symbol": "BTC/USDT:USDT",
                "side": "sell",
                "quantity": 1.0,
                "order_type": "market",
                "position_effect": "open",
                "reference_price": 50_000.0,
            }
        )


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


def test_place_order_forwards_client_order_id(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.create_order.return_value = {"id": "ord_1", "status": "open"}
    signal = {
        "symbol": "BTC/USDT",
        "side": "buy",
        "quantity": 0.01,
        "order_type": "market",
        "client_order_id": "strat-BTCUSDT-open-20260101T000000",
    }
    authed_adapter.place_order(signal)
    assert (
        mock_ccxt_exchange.create_order.call_args.kwargs["params"]["clientOrderId"]
        == "strat-BTCUSDT-open-20260101T000000"
    )


def test_place_order_without_client_order_id_omits_param(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.create_order.return_value = {"id": "ord_1", "status": "open"}
    signal = {
        "symbol": "BTC/USDT",
        "side": "buy",
        "quantity": 0.01,
        "order_type": "market",
    }
    authed_adapter.place_order(signal)
    assert "clientOrderId" not in mock_ccxt_exchange.create_order.call_args.kwargs["params"]


def test_find_order_uses_client_id_across_order_history(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.has = {"fetchOrders": True}
    mock_ccxt_exchange.fetch_orders.return_value = [
        {"id": "other", "clientOrderId": "other"},
        {"id": "ord_1", "clientOrderId": "strategy-1"},
    ]

    result = authed_adapter.find_order("strategy-1", "BTC/USDT")

    assert result == {"id": "ord_1", "clientOrderId": "strategy-1"}
    mock_ccxt_exchange.fetch_orders.assert_called_once_with("BTC/USDT")


def test_cancel_order_returns_refreshed_cumulative_state(authed_adapter, mock_ccxt_exchange):
    mock_ccxt_exchange.has = {"cancelOrder": True, "fetchOrder": True}
    mock_ccxt_exchange.fetch_order.return_value = {
        "id": "ord_1",
        "status": "canceled",
        "filled": 0.5,
    }

    result = authed_adapter.cancel_order("ord_1", "BTC/USDT")

    mock_ccxt_exchange.cancel_order.assert_called_once_with("ord_1", "BTC/USDT")
    mock_ccxt_exchange.fetch_order.assert_called_once_with("ord_1", "BTC/USDT")
    assert result["filled"] == 0.5


# ---------------------------------------------------------------------------
# Test 5: sandbox mode — CryptoAdapter.__init__ itself (not the bypassed
# __new__ fixtures above), since that's the only place set_sandbox_mode /
# the binance URL patch actually run.
# ---------------------------------------------------------------------------


def _build_adapter_via_init(exchange_id: str, mock_exchange: MagicMock, **kwargs) -> CryptoAdapter:
    with patch("brokers.crypto_adapter._require_ccxt") as mock_require_ccxt:
        mock_exchange_cls = MagicMock(return_value=mock_exchange)
        mock_require_ccxt.return_value = MagicMock(**{exchange_id: mock_exchange_cls})
        return CryptoAdapter(
            exchange_id=exchange_id, api_key="k", api_secret="s", sandbox=True, **kwargs
        )


def test_sandbox_enables_ccxt_sandbox_mode():
    mock_exchange = MagicMock()
    mock_exchange.urls = {"api": {"public": "https://testnet.binance.vision/api/v3"}}
    _build_adapter_via_init("binance", mock_exchange)
    mock_exchange.set_sandbox_mode.assert_called_once_with(True)


def test_sandbox_patches_deprecated_binance_testnet_url():
    """Regression: ccxt's set_sandbox_mode() for binance still points to the
    deprecated testnet.binance.vision (ccxt/ccxt#27266, open as of 2026-07).
    Binance migrated Spot Testnet ("Demo Trading") to demo-api.binance.com;
    the old host no longer accepts authenticated requests."""
    mock_exchange = MagicMock()
    mock_exchange.urls = {
        "api": {
            "public": "https://testnet.binance.vision/api/v3",
            "private": "https://testnet.binance.vision/api/v3",
            # different domain (futures testnet) — must be left untouched
            "fapiPublic": "https://testnet.binancefuture.com/fapi/v1",
        }
    }
    _build_adapter_via_init("binance", mock_exchange)
    assert mock_exchange.urls["api"]["public"] == "https://demo-api.binance.com/api/v3"
    assert mock_exchange.urls["api"]["private"] == "https://demo-api.binance.com/api/v3"
    assert mock_exchange.urls["api"]["fapiPublic"] == "https://testnet.binancefuture.com/fapi/v1"


def test_sandbox_patch_not_applied_to_non_binance_exchange():
    mock_exchange = MagicMock()
    mock_exchange.urls = {"api": {"public": "https://testnet.example.com/api/v3"}}
    _build_adapter_via_init("okx", mock_exchange)
    assert mock_exchange.urls["api"]["public"] == "https://testnet.example.com/api/v3"
