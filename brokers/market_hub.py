"""MarketHub — multi-market adapter dispatcher.

Routes ``fetch_ohlcv``, ``execute_signal``, and ``get_position`` calls
to the correct adapter based on a ``market`` key (e.g. ``"CRYPTO"``).
"""

from __future__ import annotations

from typing import Any

import pandas as pd


class MarketHub:
    """Dispatch market operations to registered adapters.

    Parameters
    ----------
    adapters : dict[str, Any] | None
        Mapping of market name → adapter instance.  If *None*, a default
        ``{"CRYPTO": CryptoAdapter()}`` is created (requires ccxt).
    """

    def __init__(self, adapters: dict[str, Any] | None = None) -> None:
        if adapters is None:
            from .crypto_adapter import CryptoAdapter
            adapters = {"CRYPTO": CryptoAdapter()}
        self._adapters: dict[str, Any] = adapters

    # ------------------------------------------------------------------

    def _get_adapter(self, market: str) -> Any:
        market_upper = market.upper()
        if market_upper not in self._adapters:
            available = ", ".join(sorted(self._adapters)) or "(none)"
            raise KeyError(
                f"Unknown market '{market}'. Available: {available}"
            )
        return self._adapters[market_upper]

    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        limit: int = 200,
    ) -> pd.DataFrame:
        """Fetch OHLCV via the adapter registered for *market*."""
        adapter = self._get_adapter(market)
        return adapter.fetch_ohlcv(symbol, timeframe, limit=limit)

    def execute_signal(self, signal: dict) -> dict:
        """Route a trading signal to the correct market adapter.

        *signal* must contain a ``"market"`` key.
        """
        market = signal.get("market")
        if market is None:
            raise ValueError("signal must contain a 'market' key")
        adapter = self._get_adapter(market)
        return adapter.place_order(signal)

    def get_position(self, market: str, symbol: str) -> dict:
        """Query position via the adapter registered for *market*."""
        adapter = self._get_adapter(market)
        return adapter.get_position(symbol)
