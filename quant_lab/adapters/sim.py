from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence
from uuid import uuid4

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
    OrderStatus,
    OrderType,
    Position,
    TradeTick,
)


class SimMarketDataAdapter(MarketDataAdapter):
    def __init__(self, adapter_id: str, venue: str, market_type: str, price_unit: str, size_unit: str) -> None:
        self._info = AdapterInfo(adapter_id=adapter_id, venue=venue, market_type=market_type)
        self._connected = False
        self._price_unit = price_unit
        self._size_unit = size_unit

    def info(self) -> AdapterInfo:
        return self._info

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def subscribe_l1(self, symbol: str, callback: Callable[[L1Quote], None]) -> None:
        now = datetime.now(timezone.utc)
        callback(
            L1Quote(
                symbol=symbol,
                ts_event=now,
                bid_price=100.0,
                ask_price=100.2,
                bid_size=1.0,
                ask_size=1.0,
                price_unit=self._price_unit,
                size_unit=self._size_unit,
            )
        )

    async def subscribe_trades(self, symbol: str, callback: Callable[[TradeTick], None]) -> None:
        now = datetime.now(timezone.utc)
        callback(
            TradeTick(
                symbol=symbol,
                ts_event=now,
                price=100.1,
                size=1.0,
                side=OrderSide.BUY,
                price_unit=self._price_unit,
                size_unit=self._size_unit,
            )
        )

    async def fetch_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Sequence[Bar]:
        bars: list[Bar] = []
        step = timedelta(hours=1 if timeframe.lower() in {"h1", "1h", "60m"} else 1)
        ts = start
        px = 100.0
        while ts < end:
            close = px * 1.001
            bars.append(
                Bar(
                    symbol=symbol,
                    ts_open=ts,
                    ts_close=ts + step,
                    timeframe=timeframe,
                    open=px,
                    high=max(px, close) * 1.001,
                    low=min(px, close) * 0.999,
                    close=close,
                    volume=10.0,
                    price_unit=self._price_unit,
                    volume_unit=self._size_unit,
                )
            )
            ts += step
            px = close
        return bars


class SimOrderAdapter(OrderAdapter):
    def __init__(self, adapter_id: str, venue: str, market_type: str, price_unit: str, quantity_unit: str) -> None:
        self._info = AdapterInfo(adapter_id=adapter_id, venue=venue, market_type=market_type)
        self._orders: dict[str, Order] = {}
        self._fills_cb: Callable[[Fill], None] | None = None
        self._price_unit = price_unit
        self._quantity_unit = quantity_unit

    def info(self) -> AdapterInfo:
        return self._info

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        quantity_unit: str,
        limit_price: float | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
    ) -> Order:
        now = datetime.now(timezone.utc)
        order_id = f"ord-{uuid4().hex[:10]}"
        fill_px = limit_price if limit_price is not None else 100.0
        order = Order(
            order_id=order_id,
            client_order_id=client_order_id or order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            status=OrderStatus.FILLED,
            quantity=float(quantity),
            filled_quantity=float(quantity),
            limit_price=limit_price,
            stop_price=stop_price,
            avg_fill_price=fill_px,
            ts_created=now,
            ts_updated=now,
            quantity_unit=quantity_unit or self._quantity_unit,
            price_unit=self._price_unit,
        )
        self._orders[order_id] = order
        if self._fills_cb is not None:
            self._fills_cb(
                Fill(
                    fill_id=f"fill-{uuid4().hex[:10]}",
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    ts_event=now,
                    price=float(fill_px),
                    quantity=float(quantity),
                    price_unit=self._price_unit,
                    quantity_unit=quantity_unit or self._quantity_unit,
                )
            )
        return order

    async def cancel_order(self, order_id: str) -> Order:
        existing = self._orders[order_id]
        updated = replace(existing, status=OrderStatus.CANCELLED, ts_updated=datetime.now(timezone.utc))
        self._orders[order_id] = updated
        return updated

    async def get_order(self, order_id: str) -> Order:
        return self._orders[order_id]

    async def subscribe_fills(self, callback: Callable[[Fill], None]) -> None:
        self._fills_cb = callback


class SimAccountAdapter(AccountAdapter):
    def __init__(self, adapter_id: str, venue: str, market_type: str, quantity_unit: str, price_unit: str) -> None:
        self._info = AdapterInfo(adapter_id=adapter_id, venue=venue, market_type=market_type)
        self._quantity_unit = quantity_unit
        self._price_unit = price_unit

    def info(self) -> AdapterInfo:
        return self._info

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_positions(self) -> Sequence[Position]:
        return [
            Position(
                symbol="MXFR1",
                side=OrderSide.BUY,
                quantity=1.0,
                avg_entry_price=100.0,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                ts_updated=datetime.now(timezone.utc),
                quantity_unit=self._quantity_unit,
                price_unit=self._price_unit,
                pnl_unit=self._price_unit,
            )
        ]

    async def get_balance(self) -> dict[str, float]:
        return {self._price_unit: 100000.0}
