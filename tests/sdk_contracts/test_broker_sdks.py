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
    assert callable(ib_async.IB.reqCompletedOrders)
    assert callable(ib_async.IB.reqExecutions)
    assert callable(ib_async.IB.cancelOrder)
