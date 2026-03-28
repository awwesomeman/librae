"""CryptoAdapter — CCXT-based market adapter for crypto exchanges.

Wraps CCXT to provide a simple duck-typed interface compatible with
signal_monitor's ``fetch_ohlcv`` protocol, plus optional order/position
methods when API credentials are supplied.

Credentials can be passed explicitly or loaded from environment variables
using the ``CRYPTO_`` prefix convention (``CRYPTO_API_KEY``,
``CRYPTO_API_SECRET``, ``CRYPTO_EXCHANGE_ID``, ``CRYPTO_SANDBOX``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .base import AdapterInfo, CredentialConfig


def _require_ccxt() -> object:
    """Import and return ccxt, raising a friendly error if missing."""
    try:
        import ccxt
        return ccxt
    except ImportError as e:
        raise ImportError(
            "ccxt is required for CryptoAdapter. "
            "Install it with: pip install ccxt"
        ) from e


@dataclass
class CryptoCredentials(CredentialConfig):
    """Credentials for a CCXT-backed exchange."""

    api_key: str = ""
    api_secret: str = ""
    exchange_id: str = "binance"
    sandbox: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.sandbox, str):
            self.sandbox = self.sandbox.lower() == "true"


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
    credentials : CryptoCredentials | None
        Alternative to individual params.  When given, the explicit
        params above are ignored.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        sandbox: bool = False,
        credentials: CryptoCredentials | None = None,
    ) -> None:
        if credentials is not None:
            exchange_id = credentials.exchange_id if credentials.exchange_id else exchange_id
            api_key = credentials.api_key if credentials.api_key else api_key
            api_secret = credentials.api_secret if credentials.api_secret else api_secret
            sandbox = credentials.sandbox or sandbox

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
        self._exchange_id = exchange_id

    def info(self) -> AdapterInfo:
        """Return adapter metadata (consistent with ABC adapters)."""
        return AdapterInfo(
            adapter_id=f"crypto_{self._exchange_id}",
            venue=self._exchange_id.upper(),
            market_type="spot",
        )

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
