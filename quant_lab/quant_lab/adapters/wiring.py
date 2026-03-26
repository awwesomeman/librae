from __future__ import annotations

from dataclasses import dataclass

from .base import AccountAdapter, MarketDataAdapter, OrderAdapter
from .binance import BinanceAccountAdapter, BinanceMarketDataAdapter, BinanceOrderAdapter
from .shioaji import ShioajiAccountAdapter, ShioajiMarketDataAdapter, ShioajiOrderAdapter


@dataclass(frozen=True)
class AdapterBundle:
    market_data: MarketDataAdapter
    order: OrderAdapter
    account: AccountAdapter


def build_adapter_bundle(venue: str, market_type: str = "paper") -> AdapterBundle:
    v = venue.strip().lower()
    if v == "binance":
        return AdapterBundle(
            market_data=BinanceMarketDataAdapter(market_type=market_type),
            order=BinanceOrderAdapter(market_type=market_type),
            account=BinanceAccountAdapter(market_type=market_type),
        )
    if v == "shioaji":
        return AdapterBundle(
            market_data=ShioajiMarketDataAdapter(market_type=market_type),
            order=ShioajiOrderAdapter(market_type=market_type),
            account=ShioajiAccountAdapter(market_type=market_type),
        )
    raise ValueError(f"Unsupported venue: {venue}")
