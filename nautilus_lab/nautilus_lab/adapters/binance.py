"""Binance adapter stubs.

Implements MarketDataAdapter, OrderAdapter, and AccountAdapter against the
Binance Spot/Futures REST+WebSocket APIs via ccxt.

MVP: all methods raise NotImplementedError until wired to ccxt.
Reserved slots for commission and slippage are present in returned objects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional, Sequence

from .base import (
    AccountAdapter,
    AdapterInfo,
    Bar,
    Fill,
    L1Quote,
    MarketDataAdapter,
    Order,
    OrderAdapter,
    OrderSide,
    OrderType,
    Position,
    TradeTick,
)

_VENUE = "BINANCE"
_SCHEMA_VERSION = "1.0.0"


class BinanceMarketDataAdapter(MarketDataAdapter):
    """Binance market data — stub for MVP."""

    def __init__(self, market_type: str = "spot") -> None:
        self._market_type = market_type

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id="binance_market_data",
            venue=_VENUE,
            market_type=self._market_type,
            schema_version=_SCHEMA_VERSION,
        )

    async def connect(self) -> None:
        raise NotImplementedError("BinanceMarketDataAdapter.connect not yet implemented")

    async def disconnect(self) -> None:
        raise NotImplementedError("BinanceMarketDataAdapter.disconnect not yet implemented")

    async def subscribe_l1(
        self,
        symbol: str,
        callback: Callable[[L1Quote], None],
    ) -> None:
        raise NotImplementedError("BinanceMarketDataAdapter.subscribe_l1 not yet implemented")

    async def subscribe_trades(
        self,
        symbol: str,
        callback: Callable[[TradeTick], None],
    ) -> None:
        raise NotImplementedError("BinanceMarketDataAdapter.subscribe_trades not yet implemented")

    async def fetch_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        raise NotImplementedError("BinanceMarketDataAdapter.fetch_bars not yet implemented")


class BinanceOrderAdapter(OrderAdapter):
    """Binance order management — stub for MVP."""

    def __init__(self, market_type: str = "spot") -> None:
        self._market_type = market_type

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id="binance_order",
            venue=_VENUE,
            market_type=self._market_type,
            schema_version=_SCHEMA_VERSION,
        )

    async def connect(self) -> None:
        raise NotImplementedError("BinanceOrderAdapter.connect not yet implemented")

    async def disconnect(self) -> None:
        raise NotImplementedError("BinanceOrderAdapter.disconnect not yet implemented")

    async def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        quantity_unit: str,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        raise NotImplementedError("BinanceOrderAdapter.submit_order not yet implemented")

    async def cancel_order(self, order_id: str) -> Order:
        raise NotImplementedError("BinanceOrderAdapter.cancel_order not yet implemented")

    async def get_order(self, order_id: str) -> Order:
        raise NotImplementedError("BinanceOrderAdapter.get_order not yet implemented")

    async def subscribe_fills(self, callback: Callable[[Fill], None]) -> None:
        raise NotImplementedError("BinanceOrderAdapter.subscribe_fills not yet implemented")


class BinanceAccountAdapter(AccountAdapter):
    """Binance account state — stub for MVP."""

    def __init__(self, market_type: str = "spot") -> None:
        self._market_type = market_type

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id="binance_account",
            venue=_VENUE,
            market_type=self._market_type,
            schema_version=_SCHEMA_VERSION,
        )

    async def connect(self) -> None:
        raise NotImplementedError("BinanceAccountAdapter.connect not yet implemented")

    async def disconnect(self) -> None:
        raise NotImplementedError("BinanceAccountAdapter.disconnect not yet implemented")

    async def get_positions(self) -> Sequence[Position]:
        raise NotImplementedError("BinanceAccountAdapter.get_positions not yet implemented")

    async def get_balance(self) -> dict[str, float]:
        raise NotImplementedError("BinanceAccountAdapter.get_balance not yet implemented")
