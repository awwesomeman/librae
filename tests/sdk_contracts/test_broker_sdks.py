"""Offline compatibility checks for optional broker SDKs.

These tests import and construct SDK data objects only. They never log in,
open a broker connection, fetch market data, or submit an order.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.sdk_contract


def test_ccxt_contract() -> None:
    from librae.brokers.crypto_adapter import _require_ccxt

    ccxt = _require_ccxt()
    exchange = ccxt.binance()

    assert exchange.id == "binance"
    assert callable(exchange.fetch_ohlcv)
    assert callable(exchange.create_order)
    assert callable(exchange.fetch_orders)
    assert callable(exchange.fetch_order)
    assert callable(exchange.fetch_open_orders)
    assert callable(exchange.cancel_order)


def test_ccxt_standin_matches_ccxt() -> None:
    from librae.brokers.crypto_adapter import _require_ccxt
    from tests.ccxt_standin import PRECISION_MODES, errors

    ccxt = _require_ccxt()

    def lineage(cls: type) -> list[str]:
        return [ancestor.__name__ for ancestor in cls.__mro__]

    standins = vars(errors)
    assert {name: lineage(cls) for name, cls in standins.items()} == {
        name: lineage(getattr(ccxt, name)) for name in standins
    }
    assert {name: getattr(ccxt, name) for name in PRECISION_MODES} == PRECISION_MODES


def test_ccxt_cancel_in_progress_reads_as_pending() -> None:
    from librae.brokers.crypto_adapter import _require_ccxt
    from librae.live.executor import LiveExecutor

    ccxt = _require_ccxt()

    assert ccxt.binance().parse_order_status("PENDING_CANCEL") == "canceling"
    assert ccxt.htx().parse_order_status("11") == "canceling"
    assert LiveExecutor._normalize_status("canceling") == "cancel_pending"


def test_shioaji_contract() -> None:
    from librae.brokers.shioaji_adapter import _require_shioaji

    sj = _require_shioaji()
    common = {
        "price": 0,
        "quantity": 1,
        "action": sj.Action.Buy,
        "order_type": sj.OrderType.IOC,
    }

    stock_order = sj.StockOrder(
        price_type=sj.StockPriceType.MKT,
        custom_field="ABC123",
        **common,
    )
    futures_order = sj.FuturesOrder(
        price_type=sj.FuturesPriceType.MKT,
        custom_field="ABC123",
        **common,
    )

    assert stock_order.quantity == 1
    assert futures_order.quantity == 1
    assert stock_order.custom_field == "ABC123"
    assert callable(sj.Shioaji.update_status)
    assert callable(sj.Shioaji.list_trades)
    assert callable(sj.Shioaji.cancel_order)
    for method in (
        "subscribe",
        "unsubscribe",
        "set_on_tick_fop_v1_callback",
        "set_event_callback",
        "set_session_down_callback",
    ):
        assert callable(getattr(sj.Shioaji, method))
    for field in ("code", "datetime", "close", "volume", "total_volume", "simtrade"):
        assert hasattr(sj.TickFOPv1, field)
    assert sj.QuoteType("tick") == sj.QuoteType.Tick


def test_ib_async_contract() -> None:
    from librae.brokers.ibkr_adapter import _require_ib_async

    ib_async = _require_ib_async()
    market_order = ib_async.MarketOrder("BUY", 1)
    limit_order = ib_async.LimitOrder("SELL", 1, 100.0)
    stock = ib_async.Stock("MU", "SMART", "USD")
    future = ib_async.Future("ES", exchange="CME", currency="USD")

    assert market_order.orderType == "MKT"
    assert limit_order.orderType == "LMT"
    assert stock.symbol == "MU"
    assert future.exchange == "CME"
    assert callable(ib_async.IB.trades)
    assert callable(ib_async.IB.openTrades)
    assert callable(ib_async.IB.isConnected)
    assert callable(ib_async.IB.reqCompletedOrders)
    assert callable(ib_async.IB.reqExecutions)
    assert callable(ib_async.IB.cancelOrder)


@pytest.mark.parametrize(
    ("ib_status_first", "code", "reported", "normalized"),
    [
        # 404: IB holds the order while shares for the short sale are located.
        pytest.param(False, 404, "PendingSubmit", "submitted", id="404-before-ib-status"),
        pytest.param(True, 404, "Submitted", "accepted", id="404-after-ib-status"),
        # 321: IB failed to validate the request.
        pytest.param(False, 321, "rejected", "rejected", id="321-before-ib-status"),
        pytest.param(True, 321, "Submitted", "accepted", id="321-after-ib-status"),
    ],
)
def test_ib_async_warning_is_read_by_its_code_and_what_ib_said(
    ib_status_first, code, reported, normalized
) -> None:
    """ib_async's own placement and warning handling, driven offline."""
    from librae.brokers.ibkr_adapter import IBKRAdapter, _require_ib_async
    from librae.live.executor import LiveExecutor

    ib_async = _require_ib_async()
    ib = ib_async.IB()
    ib.client.placeOrder = lambda *_args: None  # nothing leaves the process
    trade = ib.placeOrder(
        ib_async.Stock("MU", "SMART", "USD"), ib_async.LimitOrder("SELL", 1, 100.0, orderId=7)
    )
    client_id = trade.order.clientId
    if ib_status_first:
        ib.wrapper.orderStatus(7, "Submitted", 0.0, 1.0, 0.0, 99, 0, 0.0, client_id, "")
    ib.wrapper.error(7, code, f"IB message {code}", "")

    assert trade.orderStatus.status == "ValidationError"
    raw = IBKRAdapter._trade_to_order(trade)["status"]
    assert raw == reported
    assert LiveExecutor._normalize_status(raw) == normalized
