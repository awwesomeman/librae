"""Unified adapter interfaces for market_data, order, and account.

All adapters must implement the relevant ABC. Concrete adapters
(Binance, Shioaji, etc.) live in sibling modules.

Naming conventions:
- All field names: strict snake_case
- No units encoded in key names; each quantity field has a companion *_unit field
- Cost/slippage fields reserved but may be None until populated
"""

from __future__ import annotations

import dataclasses
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import TracebackType
from typing import Callable, Optional, Self, Sequence


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Canonical data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterInfo:
    """Static metadata about an adapter instance."""

    adapter_id: str
    venue: str
    market_type: str
    schema_version: str = "1.0.0"


@dataclass(frozen=True)
class L1Quote:
    """Best bid/ask snapshot."""

    symbol: str
    ts_event: datetime
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    price_unit: str
    size_unit: str


@dataclass(frozen=True)
class TradeTick:
    """Single executed trade from the exchange."""

    symbol: str
    ts_event: datetime
    price: float
    size: float
    side: OrderSide
    price_unit: str
    size_unit: str


@dataclass(frozen=True)
class Bar:
    """OHLCV bar."""

    symbol: str
    ts_open: datetime
    ts_close: datetime
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    price_unit: str
    volume_unit: str


@dataclass(frozen=True)
class Order:
    """Canonical order representation (pre/post submission)."""

    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    quantity: float
    filled_quantity: float
    limit_price: Optional[float]
    stop_price: Optional[float]
    avg_fill_price: Optional[float]
    ts_created: datetime
    ts_updated: datetime
    quantity_unit: str
    price_unit: str
    # Reserved: populated after execution
    commission: Optional[float] = None
    commission_unit: Optional[str] = None
    slippage: Optional[float] = None
    slippage_unit: Optional[str] = None


@dataclass(frozen=True)
class Fill:
    """Single fill event for an order."""

    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    ts_event: datetime
    price: float
    quantity: float
    price_unit: str
    quantity_unit: str
    # Reserved for cost/slippage
    commission: Optional[float] = None
    commission_unit: Optional[str] = None
    slippage: Optional[float] = None
    slippage_unit: Optional[str] = None


@dataclass
class Position:
    """Current position state."""

    symbol: str
    side: OrderSide
    quantity: float
    avg_entry_price: float
    unrealized_pnl: Optional[float]
    realized_pnl: Optional[float]
    ts_updated: datetime
    quantity_unit: str
    price_unit: str
    pnl_unit: str


# ---------------------------------------------------------------------------
# Credential config
# ---------------------------------------------------------------------------


@dataclass
class CredentialConfig:
    """Base credential config with env-var loading.

    Subclass and add fields for each venue.  Call ``from_env()`` to
    populate fields from environment variables automatically.

    Env-var mapping convention: ``{PREFIX}_{FIELD_UPPER}``.
    E.g. ``BINANCE_API_KEY`` for field ``api_key`` with prefix ``BINANCE``.
    """

    @classmethod
    def from_env(cls, prefix: str, **overrides: str) -> Self:
        """Build credentials from environment variables.

        For each dataclass field, looks up ``{prefix}_{FIELD_UPPER}``
        in ``os.environ``.  Explicit *overrides* take precedence.
        """
        _sentinel = object()
        kwargs: dict = {}
        for f in dataclasses.fields(cls):
            override = overrides.get(f.name, _sentinel)
            if override is not _sentinel:
                kwargs[f.name] = override
            else:
                env_val = os.environ.get(f"{prefix}_{f.name.upper()}")
                if env_val is not None:
                    kwargs[f.name] = env_val
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Abstract interfaces
# ---------------------------------------------------------------------------


class _ConnectableMixin:
    """Shared async-context-manager and connection state for all adapters."""

    _connected: bool = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._connected:
            await self.disconnect()


class MarketDataAdapter(_ConnectableMixin, ABC):
    """Interface for real-time and historical market data."""

    @abstractmethod
    def info(self) -> AdapterInfo:
        """Return adapter metadata."""

    @abstractmethod
    async def subscribe_l1(
        self,
        symbol: str,
        callback: Callable[[L1Quote], None],
    ) -> None:
        """Subscribe to L1 (BBO) updates for a symbol."""

    @abstractmethod
    async def subscribe_trades(
        self,
        symbol: str,
        callback: Callable[[TradeTick], None],
    ) -> None:
        """Subscribe to trade tick updates for a symbol."""

    @abstractmethod
    async def fetch_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        """Fetch historical OHLCV bars."""


class OrderAdapter(_ConnectableMixin, ABC):
    """Interface for order submission and lifecycle management."""

    @abstractmethod
    def info(self) -> AdapterInfo:
        """Return adapter metadata."""

    @abstractmethod
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
        """Submit an order. Returns the canonical Order with exchange-assigned id."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an open order. Returns updated Order."""

    @abstractmethod
    async def get_order(self, order_id: str) -> Order:
        """Fetch current order state."""

    @abstractmethod
    async def subscribe_fills(
        self,
        callback: Callable[[Fill], None],
    ) -> None:
        """Subscribe to fill events (all symbols for this account)."""


class AccountAdapter(_ConnectableMixin, ABC):
    """Interface for account/portfolio state queries."""

    @abstractmethod
    def info(self) -> AdapterInfo:
        """Return adapter metadata."""

    @abstractmethod
    async def get_positions(self) -> Sequence[Position]:
        """Return all current positions."""

    @abstractmethod
    async def get_balance(self) -> dict[str, float]:
        """Return account balances keyed by asset/currency symbol."""
