"""CryptoAdapter — CCXT-based market adapter for crypto exchanges.

Wraps CCXT to provide a simple duck-typed interface compatible with
signal_monitor's ``fetch_ohlcv`` protocol, plus optional order/position
methods when API credentials are supplied.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd


def _require_ccxt():
    """Import and return ccxt, raising a friendly error if missing."""
    try:
        import ccxt
        return ccxt
    except ImportError:
        raise ImportError(
            "ccxt is required for CryptoAdapter. "
            "Install it with: pip install ccxt"
        )


class CryptoAdapter:
    """Crypto exchange adapter backed by CCXT.

    Parameters
    ----------
    exchange_id : str
        CCXT exchange id (default ``"binance"``).
    api_key, api_secret : str
        Exchange credentials. Empty strings → read-only mode.
    sandbox : bool
        If True, enable the exchange sandbox/testnet.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        sandbox: bool = False,
    ) -> None:
        ccxt = _require_ccxt()
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unknown CCXT exchange: {exchange_id}")

        config: dict[str, Any] = {"enableRateLimit": True}
        if api_key:
            config["apiKey"] = api_key
        if api_secret:
            config["secret"] = api_secret

        self._exchange = exchange_class(config)
        if sandbox:
            self._exchange.set_sandbox_mode(True)

        self._read_only = not bool(api_key)
        self.exchange_id = exchange_id

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles and return a standardised DataFrame.

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is a UTC-aware ``datetime``.
        """
        raw = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df

    # ------------------------------------------------------------------
    # Order management (requires API key)
    # ------------------------------------------------------------------

    def _require_auth(self) -> None:
        if self._read_only:
            raise NotImplementedError(
                "API key not configured — CryptoAdapter is in read-only mode. "
                "Provide api_key/api_secret to enable trading."
            )

    def place_order(self, signal: dict) -> dict:
        """Place an order.

        Expected *signal* keys: ``symbol``, ``side``, ``quantity``,
        ``order_type`` (``"market"`` or ``"limit"``), and optionally
        ``price`` for limit orders.
        """
        self._require_auth()
        order_type = signal.get("order_type", "market")
        price = signal.get("price")
        result = self._exchange.create_order(
            symbol=signal["symbol"],
            type=order_type,
            side=signal["side"],
            amount=signal["quantity"],
            price=price,
        )
        return result

    def get_position(self, symbol: str) -> dict:
        """Return current position for *symbol*.

        Returns ``{symbol, size, avg_price, unrealized_pnl}``.
        """
        self._require_auth()
        positions = self._exchange.fetch_positions([symbol])
        for pos in positions:
            if pos.get("symbol") == symbol:
                return {
                    "symbol": symbol,
                    "size": float(pos.get("contracts", 0)),
                    "avg_price": float(pos.get("entryPrice", 0) or 0),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                }
        return {"symbol": symbol, "size": 0, "avg_price": 0, "unrealized_pnl": 0}
