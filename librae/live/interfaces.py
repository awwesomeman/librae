"""Typed integration boundaries for the live engine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import pandas as pd

from librae.core.executor import OrderEvent
from librae.core.funding import FundingCashFlow


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
        daily_pnl: float,
        position: str,
    ) -> object: ...


type BarCallback = Callable[
    [str, datetime, str, str, float, float, float, float, float, float, float],
    None,
]
type OrderEventCallback = Callable[[OrderEvent, int], None]
type OhlcvCallback = Callable[[str, str, dict[str, float], datetime], None]
type HeartbeatCallback = Callable[[str], None]
type FundingCashFlowCallback = Callable[[FundingCashFlow], None]
type PerformanceCallback = Callable[[str, str], None]
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
