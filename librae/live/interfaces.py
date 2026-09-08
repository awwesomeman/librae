"""Typed integration boundaries for the live engine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import pandas as pd

from librae.core.executor import PositionEvent, RuntimeEvent
from librae.core.financing import FinancingCashFlow


class BarDataFetcher(Protocol):
    """Return recent bars for one canonical symbol."""

    def __call__(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        *,
        drop_incomplete: bool = False,
    ) -> pd.DataFrame: ...


class MarketDataRouteOwner(Protocol):
    """Optional capability declaring which native adapter contract is used.

    A caller-owned fetcher that does not expose this capability owns its own
    normalization contract. Repository adapters expose it so route-specific
    startup validation and argument binding apply only when they are actually
    in the market-data path.
    """

    market_data_route: str


class MarketDataCalendarProvider(Protocol):
    """Optional source capability supplying a missing subscription calendar."""

    market_data_calendar_id: str


class Notifier(Protocol):
    """Operational notification transport used by ``LiveTrader``."""

    @property
    def enabled(self) -> bool: ...

    def send_signal(
        self,
        *,
        strategy: str,
        symbol: str,
        side: str,
        price: float,
        quantity: float | None = None,
        notional: float | None = None,
    ) -> object: ...

    def send_exit(
        self,
        *,
        strategy: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        net_pnl: float,
        net_return: float,
        periods_held: int,
    ) -> object: ...

    def send_batch(
        self,
        *,
        strategy: str,
        fills: list[dict[str, object]],
    ) -> object: ...

    def send_startup(
        self,
        *,
        strategy: str,
        symbol: str,
        mode: str,
        run_id: str,
    ) -> object: ...

    def send_shutdown(
        self,
        *,
        strategy: str,
        symbol: str,
        reason: str,
    ) -> object: ...

    def send_alert(self, *, title: str, message: str) -> object: ...

    def send_status(
        self,
        *,
        strategy: str,
        symbol: str,
        equity: float,
        drawdown: float,
        period_pnl: float,
        num_periods: int,
        position: str,
    ) -> object: ...


type BarCallback = Callable[
    [str, datetime, str, str, float, float, float, float, float, float, float],
    None,
]
type PositionEventCallback = Callable[[PositionEvent, int], None]
# Best-effort audit projection: fetched history may repeat after restart, while
# a delivery failure is not guaranteed to retry. Sinks must be idempotent on
# the exact subscription plus the row's (ts, available_at) version.
type OhlcvCallback = Callable[[str, str, dict[str, object], datetime], None]
type HeartbeatCallback = Callable[[str], None]
type FinancingCashFlowCallback = Callable[[FinancingCashFlow], None]
type RuntimeEventCallback = Callable[[RuntimeEvent], None]
type PerformanceCallback = Callable[[str, str], None]
# The third argument is a requested history span, not a guaranteed row count.
# LiveTrader may retry this caller-owned DB/API policy with a larger value when
# closed sessions or source limits leave the usable completed history short.
type WarmupFetcher = Callable[[str, str, int], pd.DataFrame]


class SignalOutcomeCallback(Protocol):
    def __call__(
        self,
        symbol: str,
        ts: datetime,
        signal: float,
        price: float,
        *,
        signal_type: str = "entry",
    ) -> None: ...
