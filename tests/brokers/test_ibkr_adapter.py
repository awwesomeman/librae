"""Tests for IBKRAdapter.

All tests use mocks — no real IB Gateway/TWS connection.
Marked us_live so they are skipped when ib_async is not installed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

pytestmark = pytest.mark.us_live


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(*, trading_enabled: bool = False):
    """Build an IBKRAdapter with mocked internals (no real connect())."""
    from brokers.ibkr_adapter import IBKRAdapter

    adapter = IBKRAdapter.__new__(IBKRAdapter)
    adapter._ib = MagicMock()
    adapter._read_only = not trading_enabled
    adapter._contract_cache = {}
    return adapter


def _make_bars_df():
    """Simulate ib_async.util.df(bars) output — 'date' column is
    UTC-aware (formatDate=2), not exchange-local strings."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2026-04-01 13:30:00+00:00",
                    "2026-04-01 13:31:00+00:00",
                    "2026-04-01 13:32:00+00:00",
                ],
            ),
            "open": [800.0, 801.0, 803.0],
            "high": [802.0, 804.0, 805.0],
            "low": [799.0, 800.0, 802.0],
            "close": [801.0, 803.0, 804.0],
            "volume": [1000, 1200, 900],
            "average": [800.5, 802.0, 803.5],
            "barCount": [50, 60, 45],
        }
    )


def _mock_ib_async_module(bars_df):
    mock = MagicMock()
    mock.util.df.return_value = bars_df
    return mock


# ---------------------------------------------------------------------------
# fetch_ohlcv
# ---------------------------------------------------------------------------


class TestFetchOhlcv:
    def test_returns_correct_columns(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._ib.reqHistoricalData.return_value = ["mock_bar"]

        with patch(
            "brokers.ibkr_adapter._require_ib_async",
            return_value=_mock_ib_async_module(_make_bars_df()),
        ):
            df = adapter.fetch_ohlcv("MU", "1m")

        assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
        assert len(df) == 3
        assert pd.api.types.is_datetime64_any_dtype(df["ts"])

    def test_empty_response(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._ib.reqHistoricalData.return_value = []

        with patch(
            "brokers.ibkr_adapter._require_ib_async",
            return_value=_mock_ib_async_module(pd.DataFrame()),
        ):
            df = adapter.fetch_ohlcv("MU", "1m")

        assert df.empty
        assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]

    def test_limit_trims_tail(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._ib.reqHistoricalData.return_value = ["mock_bar"]

        with patch(
            "brokers.ibkr_adapter._require_ib_async",
            return_value=_mock_ib_async_module(_make_bars_df()),
        ):
            df = adapter.fetch_ohlcv("MU", "1m", limit=2)

        assert len(df) == 2

    def test_unsupported_timeframe_raises(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")

        with pytest.raises(ValueError, match="Unsupported timeframe"):
            adapter.fetch_ohlcv("MU", "7m")

    def test_start_end_computes_duration_and_filters_range(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._ib.reqHistoricalData.return_value = ["mock_bar"]

        with patch(
            "brokers.ibkr_adapter._require_ib_async",
            return_value=_mock_ib_async_module(_make_bars_df()),
        ):
            df = adapter.fetch_ohlcv(
                "MU", "1m", start="2026-04-01T13:31:00Z", end="2026-04-01T13:32:00Z"
            )

        assert len(df) == 2  # first bar (13:30) excluded — before start
        call_kwargs = adapter._ib.reqHistoricalData.call_args.kwargs
        assert call_kwargs["durationStr"] == "1 D"


# ---------------------------------------------------------------------------
# Read-only guard
# ---------------------------------------------------------------------------


class TestReadOnlyGuard:
    def test_place_order_raises_without_trading_enabled(self):
        adapter = _make_adapter(trading_enabled=False)

        with pytest.raises(NotImplementedError, match="read-only"):
            adapter.place_order({"symbol": "MU", "side": "buy", "quantity": 1})

    def test_get_position_raises_without_trading_enabled(self):
        adapter = _make_adapter(trading_enabled=False)

        with pytest.raises(NotImplementedError, match="read-only"):
            adapter.get_position("MU")


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    def _mock_ib_async_module(self):
        mock = MagicMock()
        mock.MarketOrder.side_effect = lambda action, qty: {
            "action": action,
            "qty": qty,
            "type": "MKT",
        }
        mock.LimitOrder.side_effect = lambda action, qty, price: {
            "action": action,
            "qty": qty,
            "price": price,
            "type": "LMT",
        }
        return mock

    def test_market_order_uses_market_order_class(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        mock_trade = MagicMock()
        mock_trade.order.orderId = 123
        mock_trade.orderStatus.status = "PendingSubmit"
        adapter._ib.placeOrder.return_value = mock_trade

        with patch(
            "brokers.ibkr_adapter._require_ib_async", return_value=self._mock_ib_async_module()
        ):
            result = adapter.place_order({"symbol": "MU", "side": "buy", "quantity": 100})

        adapter._ib.placeOrder.assert_called_once_with(
            "mock_contract",
            {"action": "BUY", "qty": 100, "type": "MKT"},
        )
        assert result == {"id": "123", "status": "PendingSubmit"}

    def test_limit_order_uses_limit_order_class(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        mock_trade = MagicMock()
        mock_trade.order.orderId = 456
        mock_trade.orderStatus.status = "Submitted"
        adapter._ib.placeOrder.return_value = mock_trade

        with patch(
            "brokers.ibkr_adapter._require_ib_async", return_value=self._mock_ib_async_module()
        ):
            result = adapter.place_order(
                {
                    "symbol": "MU",
                    "side": "sell",
                    "quantity": 50,
                    "order_type": "limit",
                    "price": 900.0,
                }
            )

        adapter._ib.placeOrder.assert_called_once_with(
            "mock_contract",
            {"action": "SELL", "qty": 50, "price": 900.0, "type": "LMT"},
        )
        assert result == {"id": "456", "status": "Submitted"}


# ---------------------------------------------------------------------------
# get_position
# ---------------------------------------------------------------------------


class TestGetPosition:
    def test_found(self):
        adapter = _make_adapter(trading_enabled=True)
        mock_pos = MagicMock()
        mock_pos.contract.symbol = "MU"
        mock_pos.position = 100
        mock_pos.avgCost = 850.0
        adapter._ib.positions.return_value = [mock_pos]

        result = adapter.get_position("MU")

        assert result == {"symbol": "MU", "size": 100, "avg_price": 850.0, "unrealized_pnl": 0.0}

    def test_not_found_returns_zero(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._ib.positions.return_value = []

        result = adapter.get_position("MU")

        assert result == {"symbol": "MU", "size": 0, "avg_price": 0, "unrealized_pnl": 0.0}


# ---------------------------------------------------------------------------
# _resolve_contract
# ---------------------------------------------------------------------------


class TestResolveContract:
    def test_qualified_contract_returned(self):
        adapter = _make_adapter()
        mock_contract = MagicMock()
        adapter._ib.qualifyContracts.return_value = [mock_contract]
        mock_ib_async = MagicMock()
        mock_ib_async.Stock.return_value = "unqualified_stock"

        with patch("brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            result = adapter._resolve_contract("MU")

        assert result is mock_contract
        adapter._ib.qualifyContracts.assert_called_once_with("unqualified_stock")

    def test_unknown_symbol_raises(self):
        adapter = _make_adapter()
        adapter._ib.qualifyContracts.return_value = []
        mock_ib_async = MagicMock()

        with (
            patch("brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
            pytest.raises(ValueError, match="Unknown symbol"),
        ):
            adapter._resolve_contract("NOTREAL")

    def test_second_call_for_same_symbol_is_cached(self):
        """Regression test: qualifyContracts is a blocking IBKR round trip —
        resolving the same symbol twice (e.g. a live poll loop hitting the
        same symbols repeatedly) must not re-issue it."""
        adapter = _make_adapter()
        mock_contract = MagicMock()
        adapter._ib.qualifyContracts.return_value = [mock_contract]
        mock_ib_async = MagicMock()
        mock_ib_async.Stock.return_value = "unqualified_stock"

        with patch("brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            first = adapter._resolve_contract("MU")
            second = adapter._resolve_contract("MU")

        assert first is mock_contract
        assert second is mock_contract
        adapter._ib.qualifyContracts.assert_called_once()
