"""Shioaji unified adapters (paper/backtest-ready)."""

from __future__ import annotations

from .sim import SimAccountAdapter, SimMarketDataAdapter, SimOrderAdapter

_VENUE = "SHIOAJI"


class ShioajiMarketDataAdapter(SimMarketDataAdapter):
    def __init__(self, market_type: str = "futures") -> None:
        super().__init__(
            adapter_id="shioaji_market_data",
            venue=_VENUE,
            market_type=market_type,
            price_unit="TWD",
            size_unit="contracts",
        )


class ShioajiOrderAdapter(SimOrderAdapter):
    def __init__(self, market_type: str = "futures") -> None:
        super().__init__(
            adapter_id="shioaji_order",
            venue=_VENUE,
            market_type=market_type,
            price_unit="TWD",
            quantity_unit="contracts",
        )


class ShioajiAccountAdapter(SimAccountAdapter):
    def __init__(self, market_type: str = "futures") -> None:
        super().__init__(
            adapter_id="shioaji_account",
            venue=_VENUE,
            market_type=market_type,
            quantity_unit="contracts",
            price_unit="TWD",
        )
