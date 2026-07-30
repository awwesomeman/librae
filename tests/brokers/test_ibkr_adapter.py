"""Tests for IBKRAdapter.

All tests use mocks — no real IB Gateway/TWS connection.
The optional SDK contract is covered separately without opening a socket.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from librae.brokers.ibkr_adapter import _require_ib_async
from librae.live.executor import PositionRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _position_request(
    symbol: str,
    *,
    security_type: str = "STK",
    exchange: str | None = None,
    multiplier: float = 1.0,
    continuous_alias: bool | None = None,
    contract_month: str | None = None,
) -> PositionRequest:
    if continuous_alias is None:
        continuous_alias = security_type == "FUT" and contract_month is None
    return PositionRequest(
        symbol=symbol,
        venue_symbol=symbol,
        currency="USD",
        multiplier=multiplier,
        security_type=security_type,
        exchange=exchange,
        continuous_alias=continuous_alias,
        contract_month=contract_month,
    )


def test_missing_ib_async_names_install_extra():
    with (
        patch.dict("sys.modules", {"ib_async": None}),
        pytest.raises(ImportError, match="us-live"),
    ):
        _require_ib_async()


def _make_adapter(*, trading_enabled: bool = False):
    """Build an IBKRAdapter with mocked internals (no real connect())."""
    from librae.brokers.ibkr_adapter import IBKRAdapter

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


def test_available_symbols_lists_mnq_front_and_next_exact_contracts():
    adapter = _make_adapter()
    september = SimpleNamespace(
        contract=SimpleNamespace(
            conId=1,
            symbol="MNQ",
            localSymbol="MNQU6",
            lastTradeDateOrContractMonth="20260918",
            multiplier="2",
            currency="USD",
            exchange="CME",
        ),
        longName="Micro E-mini Nasdaq-100",
        category="Equity Index",
        subcategory="",
        minTick=0.25,
    )
    december = SimpleNamespace(
        contract=SimpleNamespace(
            conId=2,
            symbol="MNQ",
            localSymbol="MNQZ6",
            lastTradeDateOrContractMonth="20261218",
            multiplier="2",
            currency="USD",
            exchange="CME",
        ),
        longName="Micro E-mini Nasdaq-100",
        category="Equity Index",
        subcategory="",
        minTick=0.25,
    )
    adapter._ib.reqContractDetails.return_value = [december, september]
    mock_ib_async = MagicMock()
    mock_ib_async.Future.return_value = "mnq-chain-query"

    with (
        patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
        patch("librae.brokers.ibkr_adapter._utc_today", return_value=date(2026, 7, 30)),
    ):
        results = adapter.available_symbols(
            query="MNQ",
            kind="future",
            asset_class="index",
            exchange="CME",
        )

    assert [(item.native_symbol, item.contract_rank) for item in results] == [
        ("MNQU6", 0),
        ("MNQZ6", 1),
    ]
    assert results[0].canonical_symbol == "MNQ_202609"
    assert results[0].contract_month == "202609"
    mock_ib_async.Future.assert_called_once_with("MNQ", exchange="CME", currency="USD")


def test_available_symbols_resolves_nvda_spot():
    adapter = _make_adapter()
    adapter._ib.reqContractDetails.return_value = [
        SimpleNamespace(
            contract=SimpleNamespace(
                conId=4815747,
                symbol="NVDA",
                localSymbol="NVDA",
                currency="USD",
            ),
            longName="NVIDIA CORP",
            minTick=0.01,
        )
    ]
    mock_ib_async = MagicMock()
    mock_ib_async.Stock.return_value = "nvda-query"

    with patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
        results = adapter.available_symbols(
            query="NVDA",
            kind="spot",
            asset_class="equity",
        )

    assert len(results) == 1
    assert results[0].canonical_symbol == "NVDA"
    assert results[0].venue_symbol == "NVDA"
    mock_ib_async.Stock.assert_called_once_with("NVDA", "SMART", "USD")


# ---------------------------------------------------------------------------
# fetch_ohlcv
# ---------------------------------------------------------------------------


class TestFetchOhlcv:
    def test_returns_correct_columns(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._ib.reqHistoricalData.return_value = ["mock_bar"]

        with patch(
            "librae.brokers.ibkr_adapter._require_ib_async",
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
            "librae.brokers.ibkr_adapter._require_ib_async",
            return_value=_mock_ib_async_module(pd.DataFrame()),
        ):
            df = adapter.fetch_ohlcv("MU", "1m")

        assert df.empty
        assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]

    def test_use_rth_defaults_false_and_is_threaded_through(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._ib.reqHistoricalData.return_value = ["mock_bar"]

        with patch(
            "librae.brokers.ibkr_adapter._require_ib_async",
            return_value=_mock_ib_async_module(_make_bars_df()),
        ):
            adapter.fetch_ohlcv("MU", "1m")
            assert adapter._ib.reqHistoricalData.call_args.kwargs["useRTH"] is False

            adapter.fetch_ohlcv("MU", "1m", use_rth=True)
            assert adapter._ib.reqHistoricalData.call_args.kwargs["useRTH"] is True

    def test_limit_trims_tail(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._ib.reqHistoricalData.return_value = ["mock_bar"]

        with patch(
            "librae.brokers.ibkr_adapter._require_ib_async",
            return_value=_mock_ib_async_module(_make_bars_df()),
        ):
            df = adapter.fetch_ohlcv("MU", "1m", limit=2)

        assert len(df) == 2

    def test_drop_incomplete_applies_completed_bar_filter(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._ib.reqHistoricalData.return_value = ["mock_bar"]

        with (
            patch(
                "librae.brokers.ibkr_adapter._require_ib_async",
                return_value=_mock_ib_async_module(_make_bars_df()),
            ),
            patch(
                "librae.brokers.ibkr_adapter.drop_incomplete_ohlcv",
                side_effect=lambda df, _timeframe: df.iloc[:-1],
            ) as drop_incomplete,
        ):
            df = adapter.fetch_ohlcv("MU", "1m", drop_incomplete=True)

        assert len(df) == 2
        drop_incomplete.assert_called_once()

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
            "librae.brokers.ibkr_adapter._require_ib_async",
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
            adapter.get_position(_position_request("MU"))


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

    def test_prepare_order_uses_contract_size_and_tick_rules(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._contract_details = MagicMock(
            return_value=SimpleNamespace(
                minSize=0.1,
                sizeIncrement=0.1,
                suggestedSizeIncrement=0.1,
                minTick=0.01,
            )
        )

        prepared = adapter.prepare_order(
            {
                "symbol": "MU",
                "side": "sell",
                "quantity": 1.29,
                "order_type": "limit",
                "price": 100.001,
                "security_type": "STK",
                "currency": "USD",
            }
        )

        assert prepared["quantity"] == 1.2
        assert prepared["price"] == 100.01

    def test_market_order_uses_market_order_class(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        mock_trade = MagicMock()
        mock_trade.order.orderId = 123
        mock_trade.order.totalQuantity = 100
        mock_trade.order.orderRef = ""
        mock_trade.orderStatus.status = "PendingSubmit"
        mock_trade.orderStatus.filled = 0
        mock_trade.orderStatus.avgFillPrice = 0
        mock_trade.fills = []
        adapter._ib.placeOrder.return_value = mock_trade

        with patch(
            "librae.brokers.ibkr_adapter._require_ib_async",
            return_value=self._mock_ib_async_module(),
        ):
            result = adapter.place_order(
                {
                    "symbol": "MU",
                    "side": "buy",
                    "quantity": 100,
                    "order_type": "market",
                    "security_type": "STK",
                    "currency": "USD",
                }
            )

        adapter._ib.placeOrder.assert_called_once_with(
            "mock_contract",
            {"action": "BUY", "qty": 100, "type": "MKT"},
        )
        assert result["id"] == "123"
        assert result["status"] == "PendingSubmit"
        assert result["amount"] == 100
        assert result["filled"] == 0

    def test_limit_order_uses_limit_order_class(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        mock_trade = MagicMock()
        mock_trade.order.orderId = 456
        mock_trade.order.totalQuantity = 50
        mock_trade.order.orderRef = ""
        mock_trade.orderStatus.status = "Submitted"
        mock_trade.orderStatus.filled = 0
        mock_trade.orderStatus.avgFillPrice = 0
        mock_trade.fills = []
        adapter._ib.placeOrder.return_value = mock_trade

        with patch(
            "librae.brokers.ibkr_adapter._require_ib_async",
            return_value=self._mock_ib_async_module(),
        ):
            result = adapter.place_order(
                {
                    "symbol": "MU",
                    "side": "sell",
                    "quantity": 50,
                    "order_type": "limit",
                    "price": 900.0,
                    "security_type": "STK",
                    "currency": "USD",
                }
            )

        adapter._ib.placeOrder.assert_called_once_with(
            "mock_contract",
            {"action": "SELL", "qty": 50, "price": 900.0, "type": "LMT"},
        )
        assert result["id"] == "456"
        assert result["status"] == "Submitted"
        assert result["amount"] == 50
        assert result["filled"] == 0

    def test_client_order_id_sets_order_ref(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        mock_trade = MagicMock()
        mock_trade.order.orderId = 789
        mock_trade.orderStatus.status = "PendingSubmit"
        adapter._ib.placeOrder.return_value = mock_trade
        mock_ib_async = MagicMock()  # MarketOrder() returns a MagicMock -- orderRef assignable

        with patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            adapter.place_order(
                {
                    "symbol": "MU",
                    "side": "buy",
                    "quantity": 100,
                    "order_type": "market",
                    "security_type": "STK",
                    "currency": "USD",
                    "client_order_id": "strat-MU-open-20260101T000000",
                }
            )

        placed_order = adapter._ib.placeOrder.call_args.args[1]
        assert placed_order.orderRef == "strat-MU-open-20260101T000000"


def test_trade_normalization_uses_ibkr_cumulative_fill_and_commission():
    from librae.brokers.ibkr_adapter import IBKRAdapter

    trade = SimpleNamespace(
        order=SimpleNamespace(
            orderId=123,
            orderRef="strategy-1",
            action="BUY",
            totalQuantity=2,
        ),
        orderStatus=SimpleNamespace(
            status="PartiallyFilled",
            filled=1,
            avgFillPrice=101.0,
        ),
        contract=SimpleNamespace(symbol="MU"),
        fills=[
            SimpleNamespace(
                time=datetime(2025, 1, 1, tzinfo=UTC),
                commissionReport=SimpleNamespace(commission=0.5),
            )
        ],
    )

    result = IBKRAdapter._trade_to_order(trade)

    assert result["clientOrderId"] == "strategy-1"
    assert result["filled"] == 1.0
    assert result["average"] == 101.0
    assert result["commission"] == 0.5
    assert result["executed_at"] == datetime(2025, 1, 1, tzinfo=UTC)


def test_find_order_recovers_completed_order_from_prior_session():
    adapter = _make_adapter(trading_enabled=True)
    completed = SimpleNamespace(
        order=SimpleNamespace(
            orderId=123,
            permId=456,
            orderRef="strategy-1",
            action="BUY",
            totalQuantity=2,
        ),
        orderStatus=SimpleNamespace(status="Filled", filled=0, avgFillPrice=0),
        contract=SimpleNamespace(symbol="MU"),
        fills=[],
    )
    fill = SimpleNamespace(
        time=datetime(2025, 1, 1, tzinfo=UTC),
        execution=SimpleNamespace(orderId=123, permId=456, shares=2, price=101.0),
        commissionReport=SimpleNamespace(commission=0.5),
    )
    adapter._ib.trades.return_value = []
    adapter._ib.reqCompletedOrders.return_value = [completed]
    adapter._ib.reqExecutions.return_value = [fill]

    result = adapter.find_order("strategy-1", "MU")

    assert result is not None
    assert result["status"] == "Filled"
    assert result["filled"] == 2.0
    assert result["average"] == 101.0
    assert result["commission"] == 0.5
    adapter._ib.reqCompletedOrders.assert_called_once_with(apiOnly=True)


def test_find_order_does_not_query_history_when_session_trade_exists():
    adapter = _make_adapter(trading_enabled=True)
    trade = SimpleNamespace(
        order=SimpleNamespace(
            orderId=123,
            orderRef="strategy-1",
            action="BUY",
            totalQuantity=2,
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, avgFillPrice=0),
        contract=SimpleNamespace(symbol="MU"),
        fills=[],
    )
    adapter._ib.trades.return_value = [trade]

    result = adapter.find_order("strategy-1", "MU")

    assert result is not None
    adapter._ib.reqCompletedOrders.assert_not_called()
    adapter._ib.reqExecutions.assert_not_called()


# ---------------------------------------------------------------------------
# get_position
# ---------------------------------------------------------------------------


class TestGetPosition:
    def test_found(self):
        adapter = _make_adapter(trading_enabled=True)
        resolved = SimpleNamespace(conId=123)
        adapter._resolve_contract = MagicMock(return_value=resolved)
        mock_pos = MagicMock()
        mock_pos.contract.symbol = "MU"
        mock_pos.contract.conId = 123
        mock_pos.position = 100
        mock_pos.avgCost = 850.0
        adapter._ib.positions.return_value = [mock_pos]

        result = adapter.get_position(_position_request("MU"))

        assert result == {"symbol": "MU", "size": 100, "avg_price": 850.0, "unrealized_pnl": 0.0}

    def test_not_found_returns_zero(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(return_value=SimpleNamespace(conId=123))
        adapter._ib.positions.return_value = []

        result = adapter.get_position(_position_request("MU"))

        assert result == {"symbol": "MU", "size": 0, "avg_price": 0, "unrealized_pnl": 0.0}

    def test_futures_position_matches_resolved_contract_id_not_root_symbol(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(
            return_value=SimpleNamespace(conId=222, multiplier="50")
        )
        adapter._ib.positions.return_value = [
            SimpleNamespace(
                contract=SimpleNamespace(symbol="ES", conId=111),
                position=1,
                avgCost=6000,
            ),
            SimpleNamespace(
                contract=SimpleNamespace(symbol="ES", conId=222),
                position=-2,
                avgCost=305_000,
            ),
        ]

        result = adapter.get_position(
            _position_request(
                "ES",
                security_type="FUT",
                exchange="CME",
                multiplier=50.0,
            )
        )

        assert result == {
            "symbol": "ES",
            "size": -2,
            "avg_price": 6100,
            "unrealized_pnl": 0.0,
        }

    def test_rejects_resolved_contract_without_stable_id(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(return_value=SimpleNamespace(conId=0))

        with pytest.raises(ValueError, match="no stable conId"):
            adapter.get_position(_position_request("MU"))

    def test_rejects_future_without_contract_multiplier(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(return_value=SimpleNamespace(conId=123))

        with pytest.raises(ValueError, match="no contract multiplier"):
            adapter.get_position(
                _position_request(
                    "ES",
                    security_type="FUT",
                    exchange="CME",
                    multiplier=50.0,
                )
            )

    def test_rejects_contract_multiplier_that_disagrees_with_accounting_config(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._resolve_contract = MagicMock(
            return_value=SimpleNamespace(conId=123, multiplier="50")
        )

        with pytest.raises(ValueError, match="contract multiplier mismatch"):
            adapter.get_position(
                _position_request(
                    "ES",
                    security_type="FUT",
                    exchange="CME",
                    multiplier=5.0,
                )
            )


# ---------------------------------------------------------------------------
# _resolve_contract
# ---------------------------------------------------------------------------


class TestGetBalance:
    def test_returns_total_cash_value_for_currency(self):
        adapter = _make_adapter(trading_enabled=True)

        def _account_value(tag, currency, value):
            v = MagicMock()
            v.tag = tag
            v.currency = currency
            v.value = value
            return v

        adapter._ib.accountSummary.return_value = [
            _account_value("TotalCashValue", "USD", "12345.67"),
            _account_value("TotalCashValue", "TWD", "999.0"),
            _account_value("NetLiquidation", "USD", "50000.0"),
        ]

        result = adapter.get_balance("USD")

        assert result == {"free": 12345.67, "used": 0.0, "total": 12345.67}

    def test_no_matching_currency_fails_explicitly(self):
        adapter = _make_adapter(trading_enabled=True)
        adapter._ib.accountSummary.return_value = []

        with pytest.raises(ValueError, match="no TotalCashValue"):
            adapter.get_balance("USD")

    def test_multiple_matching_cash_values_fail_as_ambiguous(self):
        adapter = _make_adapter(trading_enabled=True)

        def account_value(value):
            return SimpleNamespace(tag="TotalCashValue", currency="USD", value=value)

        adapter._ib.accountSummary.return_value = [
            account_value("100"),
            account_value("200"),
        ]

        with pytest.raises(ValueError, match="ambiguous TotalCashValue"):
            adapter.get_balance("USD")

    def test_requires_trading_enabled(self):
        adapter = _make_adapter(trading_enabled=False)

        with pytest.raises(NotImplementedError, match="read-only"):
            adapter.get_balance("USD")


class TestResolveContract:
    def test_qualified_contract_returned(self):
        adapter = _make_adapter()
        mock_contract = MagicMock()
        adapter._ib.qualifyContracts.return_value = [mock_contract]
        mock_ib_async = MagicMock()
        mock_ib_async.Stock.return_value = "unqualified_stock"

        with patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            result = adapter._resolve_contract("MU")

        assert result is mock_contract
        adapter._ib.qualifyContracts.assert_called_once_with("unqualified_stock")

    def test_unknown_symbol_raises(self):
        adapter = _make_adapter()
        adapter._ib.qualifyContracts.return_value = []
        mock_ib_async = MagicMock()

        with (
            patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
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

        with patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            first = adapter._resolve_contract("MU")
            second = adapter._resolve_contract("MU")

        assert first is mock_contract
        assert second is mock_contract
        adapter._ib.qualifyContracts.assert_called_once()


class TestResolveContractFutures:
    @pytest.fixture(autouse=True)
    def _fixed_today(self):
        with patch("librae.brokers.ibkr_adapter._utc_today", return_value=date(2026, 1, 1)):
            yield

    def _detail(self, expiry: str, contract):
        detail = MagicMock()
        detail.contract = contract
        contract.lastTradeDateOrContractMonth = expiry
        return detail

    def test_picks_nearest_non_expired_contract(self):
        adapter = _make_adapter()
        near, far = MagicMock(), MagicMock()
        adapter._ib.reqContractDetails.return_value = [
            self._detail("20260620", far),
            self._detail("20260321", near),
        ]
        mock_ib_async = MagicMock()
        mock_ib_async.Future.return_value = "unqualified_future"

        with patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            result = adapter._resolve_contract(
                "ES", security_type="FUT", exchange="CME", continuous_alias=True
            )

        assert result is near
        mock_ib_async.Future.assert_called_once_with("ES", exchange="CME", currency="USD")

    def test_exact_contract_month_selects_only_requested_month(self):
        adapter = _make_adapter()
        september, december = MagicMock(), MagicMock()
        adapter._ib.reqContractDetails.return_value = [
            self._detail("20261218", december),
            self._detail("20260918", september),
        ]
        mock_ib_async = MagicMock()
        mock_ib_async.Future.return_value = "unqualified_future"

        with patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            result = adapter._resolve_contract(
                "ES",
                security_type="FUT",
                exchange="CME",
                contract_month="202609",
            )

        assert result is september
        mock_ib_async.Future.assert_called_once_with(
            "ES",
            exchange="CME",
            currency="USD",
            lastTradeDateOrContractMonth="202609",
        )

    def test_exact_contract_month_does_not_fall_back_to_another_month(self):
        adapter = _make_adapter()
        adapter._ib.reqContractDetails.return_value = [
            self._detail("20261218", MagicMock()),
        ]
        mock_ib_async = MagicMock()
        mock_ib_async.Future.return_value = "unqualified_future"

        with (
            patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
            pytest.raises(ValueError, match="contract_month=202609"),
        ):
            adapter._resolve_contract(
                "ES",
                security_type="FUT",
                exchange="CME",
                contract_month="202609",
            )

    def test_future_requires_explicit_selection_mode(self):
        adapter = _make_adapter()

        with pytest.raises(ValueError, match="exactly one"):
            adapter._resolve_contract("ES", security_type="FUT", exchange="CME")

    def test_future_rejects_month_and_continuous_alias_together(self):
        adapter = _make_adapter()

        with pytest.raises(ValueError, match="exactly one"):
            adapter._resolve_contract(
                "ES",
                security_type="FUT",
                exchange="CME",
                continuous_alias=True,
                contract_month="202609",
            )

    def test_exact_contract_month_rejects_ambiguous_matches(self):
        adapter = _make_adapter()
        adapter._ib.reqContractDetails.return_value = [
            self._detail("20260918", MagicMock()),
            self._detail("20260919", MagicMock()),
        ]
        mock_ib_async = MagicMock()
        mock_ib_async.Future.return_value = "unqualified_future"

        with (
            patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
            pytest.raises(ValueError, match="Ambiguous IBKR future"),
        ):
            adapter._resolve_contract(
                "ES",
                security_type="FUT",
                exchange="CME",
                contract_month="202609",
            )

    def test_ignores_expired_contract_details(self):
        adapter = _make_adapter()
        expired, near, far = MagicMock(), MagicMock(), MagicMock()
        adapter._ib.reqContractDetails.return_value = [
            self._detail("20251219", expired),
            self._detail("20260321", near),
            self._detail("20260620", far),
        ]
        mock_ib_async = MagicMock()
        mock_ib_async.Future.return_value = "unqualified_future"

        with patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            result = adapter._resolve_contract(
                "ES", security_type="FUT", exchange="CME", continuous_alias=True
            )

        assert result is near

    def test_expired_cached_front_month_is_resolved_again(self):
        adapter = _make_adapter()
        expired, current = MagicMock(), MagicMock()
        adapter._ib.reqContractDetails.side_effect = [
            [self._detail("20260321", expired)],
            [self._detail("20260620", current)],
        ]
        mock_ib_async = MagicMock()
        mock_ib_async.Future.return_value = "unqualified_future"

        with (
            patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
            patch("librae.brokers.ibkr_adapter._utc_today", return_value=date(2026, 1, 1)),
        ):
            first = adapter._resolve_contract(
                "ES", security_type="FUT", exchange="CME", continuous_alias=True
            )
        with (
            patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
            patch("librae.brokers.ibkr_adapter._utc_today", return_value=date(2026, 4, 1)),
        ):
            second = adapter._resolve_contract(
                "ES", security_type="FUT", exchange="CME", continuous_alias=True
            )

        assert first is expired
        assert second is current
        assert adapter._ib.reqContractDetails.call_count == 2

    def test_contract_details_cache_rolls_with_expired_contract(self):
        adapter = _make_adapter()
        expired, current = MagicMock(), MagicMock()
        cache_key = ("ES", "FUT", "CME", "USD", None)
        expired_detail = self._detail("20260321", expired)
        current_detail = self._detail("20260620", current)
        adapter._contract_cache[cache_key] = expired
        adapter._contract_details_cache = {cache_key: expired_detail}
        adapter._ib.reqContractDetails.return_value = [current_detail]
        mock_ib_async = MagicMock()
        mock_ib_async.Future.return_value = "unqualified_future"

        with (
            patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
            patch("librae.brokers.ibkr_adapter._utc_today", return_value=date(2026, 4, 1)),
        ):
            result = adapter._contract_details(
                "ES",
                security_type="FUT",
                exchange="CME",
                currency="USD",
                continuous_alias=True,
            )

        assert result is current_detail
        assert adapter._contract_cache[cache_key] is current

    def test_no_non_expired_contract_raises(self):
        adapter = _make_adapter()
        expired = MagicMock()
        adapter._ib.reqContractDetails.return_value = [
            self._detail("20251219", expired),
        ]
        mock_ib_async = MagicMock()
        mock_ib_async.Future.return_value = "unqualified_future"

        with (
            patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
            pytest.raises(ValueError, match="No non-expired future"),
        ):
            adapter._resolve_contract(
                "ES", security_type="FUT", exchange="CME", continuous_alias=True
            )

    def test_missing_exchange_raises(self):
        adapter = _make_adapter()

        with pytest.raises(ValueError, match="exchange is required"):
            adapter._resolve_contract("ES", security_type="FUT")

    def test_unknown_future_raises(self):
        adapter = _make_adapter()
        adapter._ib.reqContractDetails.return_value = []
        mock_ib_async = MagicMock()

        with (
            patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async),
            pytest.raises(ValueError, match="Unknown future"),
        ):
            adapter._resolve_contract(
                "NOTREAL",
                security_type="FUT",
                exchange="CME",
                continuous_alias=True,
            )

    def test_unsupported_security_type_raises(self):
        adapter = _make_adapter()

        with pytest.raises(ValueError, match="Unsupported security_type"):
            adapter._resolve_contract("ES", security_type="OPT")

    def test_stock_and_future_caches_are_independent(self):
        """Same symbol, different security_type, must not collide in the cache."""
        adapter = _make_adapter()
        stock_contract, future_contract = MagicMock(), MagicMock()
        adapter._ib.qualifyContracts.return_value = [stock_contract]
        adapter._ib.reqContractDetails.return_value = [self._detail("20260620", future_contract)]
        mock_ib_async = MagicMock()
        mock_ib_async.Stock.return_value = "unqualified_stock"
        mock_ib_async.Future.return_value = "unqualified_future"

        with patch("librae.brokers.ibkr_adapter._require_ib_async", return_value=mock_ib_async):
            stock_result = adapter._resolve_contract("ES", security_type="STK")
            future_result = adapter._resolve_contract(
                "ES", security_type="FUT", exchange="CME", continuous_alias=True
            )

        assert stock_result is stock_contract
        assert future_result is future_contract
