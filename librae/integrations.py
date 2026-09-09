"""Stable public contracts for caller-owned Librae integrations.

Third-party packages should import protocols and value types from this module
instead of depending on engine-private implementation details.
"""

from typing import Protocol

from librae.live.execution_identity import ExecutionIdentity, ExecutionIdentityProvider
from librae.live.executor import (
    BalanceReader,
    BrokerBalance,
    BrokerOrderReport,
    BrokerPosition,
    ExecutionReport,
    OrderAdapter,
    OrderRequest,
    OrderSignal,
    PositionRequest,
)
from librae.live.interfaces import (
    BarDataFetcher,
    MarketDataCalendarProvider,
    MarketDataRouteOwner,
    Notifier,
)
from librae.live.state import LiveStateStore


class AdapterFactory(Protocol):
    """Construct one data/order adapter for repository orchestration."""

    def __call__(self, *, trading: bool) -> object: ...


__all__ = [
    "AdapterFactory",
    "BalanceReader",
    "BarDataFetcher",
    "BrokerBalance",
    "BrokerOrderReport",
    "BrokerPosition",
    "ExecutionIdentity",
    "ExecutionIdentityProvider",
    "ExecutionReport",
    "LiveStateStore",
    "MarketDataCalendarProvider",
    "MarketDataRouteOwner",
    "Notifier",
    "OrderAdapter",
    "OrderRequest",
    "OrderSignal",
    "PositionRequest",
]
