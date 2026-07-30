"""Standardized backtest output schema + data contracts.

All field names: strict snake_case.
Unit fields stored alongside values for multi-market support (USDT, TWD, contracts, etc.).
Cost/slippage fields are optional (may be None for simple backtests).

Persistence is caller-selected. ``librae.artifacts`` can flatten engine output
into format-neutral tables; ``db.timescale_writer`` is the reference database
integration.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from librae.core.run_config import RunMode
from librae.core.strategy import PositionEventType, PositionSide

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-]*-\d{8}t\d{4}-[a-f0-9]{6}$")

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMetadata:
    """Identifies and describes a backtest run."""

    run_id: str
    strategy: str
    symbols: tuple[str, ...]
    timeframe: str
    data_source: str
    started_at: datetime
    ended_at: datetime
    run_at: datetime
    mode: RunMode = "backtest"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if self.mode not in ("backtest", "sim", "live"):
            raise ValueError(f"invalid run mode: {self.mode!r}")


@dataclass(frozen=True)
class EquityCurvePoint:
    """Single point on the equity curve."""

    ts: datetime
    equity: float
    period_return: float
    drawdown: float
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    concentration: float = 0.0
    turnover: float = 0.0
    strategy: str | None = None
    exposed: bool = False


@dataclass(frozen=True)
class OrderEventRecord:
    """Position lifecycle event whose costs belong only to this execution."""

    event_id: str
    ts: datetime
    account_id: str
    currency: str
    symbol: str
    side: PositionSide
    event_type: PositionEventType
    fill_quantity: float
    price: float
    entry_price: float
    remaining_quantity: float
    notional: float
    commission: float = 0.0
    slippage: float = 0.0
    tax: float = 0.0
    pnl: float | None = None
    net_return: float | None = None
    entry_at: datetime | None = None
    periods_held: int | None = None
    reason: str = ""
    entry_commission: float | None = None
    entry_slippage: float | None = None
    entry_tax: float | None = None


@dataclass(frozen=True)
class PositionSnapshotPoint:
    """One open position's end-of-bar realized portfolio weight.

    ``market_value`` and ``realized_weight`` are signed: long exposure is
    positive and short exposure is negative.
    """

    ts: datetime
    account_id: str
    currency: str
    symbol: str
    side: PositionSide
    quantity: float
    price: float
    market_value: float
    realized_weight: float


@dataclass(frozen=True)
class AllocationSnapshotPoint:
    """One symbol's target-versus-achieved portfolio weight."""

    ts: datetime
    account_id: str
    currency: str
    symbol: str
    target_weight: float | None
    realized_weight: float
    weight_drift: float | None


@dataclass(frozen=True)
class FundingCashFlowRecord:
    """One timestamped perpetual-funding payment."""

    ts: datetime
    account_id: str
    currency: str
    symbol: str
    side: PositionSide
    quantity: float
    mark_price: float
    multiplier: float
    rate: float
    cash_flow: float


@dataclass(frozen=True)
class StrategyMetrics:
    """Aggregate performance metrics for a backtest run.

    Units convention (not stored, documented here):
    - return fields: ratio (e.g. 0.05 = 5%)
    - max_drawdown: negative ratio (e.g. -0.15 = 15% decline)
    - win_rate/exposure_ratio: ratio 0-1
    - period_sharpe/period_sortino/profit_factor/payoff_ratio:
      dimensionless scores; None when their required denominator population
      is absent or zero
    - trades: count
    - avg_trade_return: notional-weighted mean net return per realized exit
    """

    # Universal (all strategies produce these)
    total_return: float
    max_drawdown: float = 0.0
    trades: int = 0

    # Full-sample period-return summary (never annualized)
    mean_period_return: float | None = None
    period_volatility: float | None = None
    period_downside_deviation: float | None = None
    period_sharpe: float | None = None
    period_sortino: float | None = None
    positive_period_rate: float | None = None

    # Most strategies (None if not applicable)
    win_rate: float | None = None
    profit_factor: float | None = None
    payoff_ratio: float | None = None
    avg_trade_return: float | None = None
    exposure_ratio: float | None = None

    # Portfolio diagnostics
    total_turnover: float | None = None
    average_gross_exposure: float | None = None
    max_gross_exposure: float | None = None
    max_abs_net_exposure: float | None = None
    max_concentration: float | None = None

    # Cost breakdown
    total_commission: float | None = None
    total_slippage: float | None = None
    total_tax: float | None = None


@dataclass(frozen=True)
class AccountPerformance:
    """Equity, PnL, and metrics for one non-netted account."""

    account_id: str
    currency: str
    initial_cash: float
    final_equity: float
    net_pnl: float
    equity_curve: Sequence[EquityCurvePoint]
    metrics: StrategyMetrics

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("account_id must be non-empty")
        if not self.currency:
            raise ValueError("currency must be non-empty")


@dataclass(frozen=True)
class BacktestOutput:
    """Top-level backtest output container.

    This is the canonical output object produced after a backtest run. Callers
    may persist it through the reference DB writer or a tabular artifact.
    """

    run_metadata: RunMetadata
    account: AccountPerformance
    order_events: Sequence[OrderEventRecord]
    position_snapshots: Sequence[PositionSnapshotPoint]
    allocation_snapshots: Sequence[AllocationSnapshotPoint]
    funding_cash_flows: Sequence[FundingCashFlowRecord] = ()

    @property
    def equity_curve(self) -> Sequence[EquityCurvePoint]:
        return self.account.equity_curve

    @property
    def metrics(self) -> StrategyMetrics:
        return self.account.metrics

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict with ISO datetime strings."""

        def _convert(obj: Any) -> Any:
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, dict):
                return {k: _convert(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_convert(item) for item in obj]
            return obj

        return _convert(asdict(self))

    def validate(self) -> None:
        """Raise ValueError if required fields are empty/missing."""
        if not self.run_metadata.run_id:
            raise ValueError("run_metadata.run_id is required")
        if not RUN_ID_PATTERN.match(self.run_metadata.run_id):
            raise ValueError(
                f"run_metadata.run_id must match pattern "
                f"'<strategy>-<symbol>-<timeframe>-<YYYYMMDDThhmm>-<hex6>', "
                f"got {self.run_metadata.run_id!r}"
            )
        if not self.run_metadata.strategy:
            raise ValueError("run_metadata.strategy is required")
        if not self.run_metadata.symbols or any(not symbol for symbol in self.run_metadata.symbols):
            raise ValueError("run_metadata.symbols must contain non-empty identifiers")
        if not self.run_metadata.timeframe:
            raise ValueError("run_metadata.timeframe is required")
        account_records = (
            *self.order_events,
            *self.position_snapshots,
            *self.allocation_snapshots,
            *self.funding_cash_flows,
        )
        for record in account_records:
            if record.account_id != self.account.account_id:
                raise ValueError(f"record references unknown account_id: {record.account_id!r}")
            if record.currency != self.account.currency:
                raise ValueError(
                    f"record currency {record.currency!r} does not match account "
                    f"{record.account_id!r} currency {self.account.currency!r}"
                )
