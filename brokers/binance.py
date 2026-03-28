"""Binance unified adapters (paper/backtest-ready)."""

from __future__ import annotations

from .sim import SimAccountAdapter, SimMarketDataAdapter, SimOrderAdapter

VENUE = "BINANCE"


class BinanceMarketDataAdapter(SimMarketDataAdapter):
    def __init__(self, market_type: str = "spot") -> None:
        super().__init__(
            adapter_id="binance_market_data",
            venue=VENUE,
            market_type=market_type,
            price_unit="USDT",
            size_unit="asset",
        )


class BinanceOrderAdapter(SimOrderAdapter):
    def __init__(self, market_type: str = "spot") -> None:
        super().__init__(
            adapter_id="binance_order",
            venue=VENUE,
            market_type=market_type,
            price_unit="USDT",
            quantity_unit="asset",
        )


class BinanceAccountAdapter(SimAccountAdapter):
    def __init__(self, market_type: str = "spot") -> None:
        super().__init__(
            adapter_id="binance_account",
            venue=VENUE,
            market_type=market_type,
            quantity_unit="asset",
            price_unit="USDT",
        )
