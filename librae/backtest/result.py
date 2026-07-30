"""Side-effect-free result models produced by the backtest engine."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from librae.core.executor import OrderEvent, TradeResult
from librae.core.funding import FundingCashFlow


@dataclass(frozen=True)
class EquitySnapshot:
    """Single bar equity snapshot."""

    ts: datetime
    equity: float


@dataclass(frozen=True)
class PositionSnapshot:
    """One open position's end-of-bar portfolio snapshot."""

    ts: datetime
    symbol: str
    side: Literal["long", "short"]
    quantity: float
    price: float
    market_value: float
    realized_weight: float


@dataclass(frozen=True)
class AllocationSnapshot:
    """One symbol's target-versus-achieved end-of-event allocation."""

    ts: datetime
    symbol: str
    target_weight: float | None
    realized_weight: float
    weight_drift: float | None


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Portfolio exposure and trading diagnostics for one event."""

    ts: datetime
    gross_exposure: float
    net_exposure: float
    concentration: float
    turnover: float
    exposed: bool


@dataclass(frozen=True)
class AccountBacktestResult:
    """Raw facts for one isolated account ledger."""

    account_id: str
    currency: str
    equity_curve: Sequence[EquitySnapshot]
    portfolio_snapshots: Sequence[PortfolioSnapshot]
    initial_cash: float
    final_equity: float
    exposed_periods: int = 0


@dataclass(frozen=True)
class BacktestResult:
    """Raw backtest facts without derived performance metrics."""

    trades: Sequence[TradeResult]
    order_events: Sequence[OrderEvent]
    position_snapshots: Sequence[PositionSnapshot]
    allocation_snapshots: Sequence[AllocationSnapshot]
    funding_cash_flows: Sequence[FundingCashFlow]
    account: AccountBacktestResult

    @property
    def equity_curve(self) -> Sequence[EquitySnapshot]:
        return self.account.equity_curve

    @property
    def portfolio_snapshots(self) -> Sequence[PortfolioSnapshot]:
        return self.account.portfolio_snapshots

    @property
    def initial_cash(self) -> float:
        return self.account.initial_cash

    @property
    def final_equity(self) -> float:
        return self.account.final_equity

    @property
    def exposed_periods(self) -> int:
        return self.account.exposed_periods
