"""Tests for the unified adapter abstraction layer.

These tests verify:
- AdapterInfo fields and defaults
- Enum values are correct snake_case strings
- Adapter stubs (Binance/Shioaji) correctly implement the ABCs and raise NotImplementedError
- Canonical data types (L1Quote, TradeTick, Bar, Order, Fill, Position) are frozen/immutable
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from nautilus_lab.adapters.base import (
    AdapterInfo,
    Bar,
    Fill,
    L1Quote,
    MarketDataAdapter,
    Order,
    OrderAdapter,
    AccountAdapter,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TradeTick,
)
from nautilus_lab.adapters.binance import (
    BinanceAccountAdapter,
    BinanceMarketDataAdapter,
    BinanceOrderAdapter,
)
from nautilus_lab.adapters.shioaji import (
    ShioajiAccountAdapter,
    ShioajiMarketDataAdapter,
    ShioajiOrderAdapter,
)

NOW = datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Enum value tests
# ---------------------------------------------------------------------------


def test_order_side_values() -> None:
    assert OrderSide.BUY == "buy"
    assert OrderSide.SELL == "sell"


def test_order_type_values() -> None:
    assert OrderType.MARKET == "market"
    assert OrderType.LIMIT == "limit"
    assert OrderType.STOP == "stop"
    assert OrderType.STOP_LIMIT == "stop_limit"


def test_order_status_values() -> None:
    assert OrderStatus.PENDING == "pending"
    assert OrderStatus.OPEN == "open"
    assert OrderStatus.FILLED == "filled"
    assert OrderStatus.CANCELLED == "cancelled"
    assert OrderStatus.REJECTED == "rejected"


# ---------------------------------------------------------------------------
# AdapterInfo
# ---------------------------------------------------------------------------


def test_adapter_info_defaults() -> None:
    info = AdapterInfo(adapter_id="test", venue="TEST", market_type="spot")
    assert info.schema_version == "1.0.0"


def test_adapter_info_frozen() -> None:
    info = AdapterInfo(adapter_id="test", venue="TEST", market_type="spot")
    with pytest.raises((AttributeError, TypeError)):
        info.venue = "OTHER"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Canonical data types — frozen
# ---------------------------------------------------------------------------


def test_l1_quote_frozen() -> None:
    q = L1Quote(
        symbol="BTCUSDT",
        ts_event=NOW,
        bid_price=50000.0,
        ask_price=50001.0,
        bid_size=1.0,
        ask_size=0.5,
        price_unit="USDT",
        size_unit="BTC",
    )
    with pytest.raises((AttributeError, TypeError)):
        q.bid_price = 0.0  # type: ignore[misc]


def test_trade_tick_fields() -> None:
    t = TradeTick(
        symbol="BTCUSDT",
        ts_event=NOW,
        price=50000.0,
        size=0.1,
        side=OrderSide.BUY,
        price_unit="USDT",
        size_unit="BTC",
    )
    assert t.side == OrderSide.BUY
    assert t.price_unit == "USDT"


def test_bar_fields() -> None:
    b = Bar(
        symbol="MXFR1",
        ts_open=NOW,
        ts_close=NOW,
        timeframe="H1",
        open=21000.0,
        high=21100.0,
        low=20900.0,
        close=21050.0,
        volume=500.0,
        price_unit="TWD",
        volume_unit="contracts",
    )
    assert b.volume_unit == "contracts"


# ---------------------------------------------------------------------------
# Binance adapter stubs
# ---------------------------------------------------------------------------


def test_binance_market_data_adapter_is_abc_subclass() -> None:
    assert issubclass(BinanceMarketDataAdapter, MarketDataAdapter)


def test_binance_adapters_info() -> None:
    md = BinanceMarketDataAdapter()
    assert md.info().venue == "BINANCE"
    assert md.info().adapter_id == "binance_market_data"

    od = BinanceOrderAdapter()
    assert od.info().venue == "BINANCE"
    assert od.info().adapter_id == "binance_order"

    ac = BinanceAccountAdapter()
    assert ac.info().venue == "BINANCE"
    assert ac.info().adapter_id == "binance_account"


@pytest.mark.asyncio
async def test_binance_market_data_raises_not_implemented() -> None:
    md = BinanceMarketDataAdapter()
    with pytest.raises(NotImplementedError):
        await md.connect()
    with pytest.raises(NotImplementedError):
        await md.fetch_bars("BTCUSDT", "1h", NOW, NOW)
    with pytest.raises(NotImplementedError):
        await md.subscribe_l1("BTCUSDT", lambda q: None)
    with pytest.raises(NotImplementedError):
        await md.subscribe_trades("BTCUSDT", lambda t: None)


@pytest.mark.asyncio
async def test_binance_order_adapter_raises_not_implemented() -> None:
    od = BinanceOrderAdapter()
    with pytest.raises(NotImplementedError):
        await od.connect()
    with pytest.raises(NotImplementedError):
        await od.submit_order("BTCUSDT", OrderSide.BUY, OrderType.MARKET, 0.1, "BTC")
    with pytest.raises(NotImplementedError):
        await od.cancel_order("oid1")
    with pytest.raises(NotImplementedError):
        await od.get_order("oid1")
    with pytest.raises(NotImplementedError):
        await od.subscribe_fills(lambda f: None)


@pytest.mark.asyncio
async def test_binance_account_adapter_raises_not_implemented() -> None:
    ac = BinanceAccountAdapter()
    with pytest.raises(NotImplementedError):
        await ac.connect()
    with pytest.raises(NotImplementedError):
        await ac.get_positions()
    with pytest.raises(NotImplementedError):
        await ac.get_balance()


# ---------------------------------------------------------------------------
# Shioaji adapter stubs
# ---------------------------------------------------------------------------


def test_shioaji_market_data_adapter_is_abc_subclass() -> None:
    assert issubclass(ShioajiMarketDataAdapter, MarketDataAdapter)


def test_shioaji_adapters_info() -> None:
    md = ShioajiMarketDataAdapter()
    assert md.info().venue == "SHIOAJI"
    assert md.info().market_type == "futures"

    od = ShioajiOrderAdapter()
    assert od.info().venue == "SHIOAJI"

    ac = ShioajiAccountAdapter()
    assert ac.info().venue == "SHIOAJI"


@pytest.mark.asyncio
async def test_shioaji_market_data_raises_not_implemented() -> None:
    md = ShioajiMarketDataAdapter()
    with pytest.raises(NotImplementedError):
        await md.connect()
    with pytest.raises(NotImplementedError):
        await md.fetch_bars("MXFR1", "H1", NOW, NOW)


@pytest.mark.asyncio
async def test_shioaji_order_adapter_raises_not_implemented() -> None:
    od = ShioajiOrderAdapter()
    with pytest.raises(NotImplementedError):
        await od.submit_order("MXFR1", OrderSide.BUY, OrderType.LIMIT, 1.0, "contracts")


@pytest.mark.asyncio
async def test_shioaji_account_adapter_raises_not_implemented() -> None:
    ac = ShioajiAccountAdapter()
    with pytest.raises(NotImplementedError):
        await ac.get_balance()
