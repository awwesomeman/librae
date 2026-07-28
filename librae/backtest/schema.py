"""Standardized backtest output schema + data contracts.

All field names: strict snake_case.
Unit fields stored alongside values for multi-market support (USDT, TWD, contracts, etc.).
Cost/slippage fields are optional (may be None for simple backtests).

Storage target: TimescaleDB via db.timescale_writer.

Also contains canonical backend data contracts:
- Schema version and validation constants
- Parsing utilities (timestamps, snake_case)
- Record validation functions
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
# Constants (from contracts.py)
# ---------------------------------------------------------------------------

SNAKE_CASE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# WHY {4,6} and {6,8}: generator now produces %H%M (4-digit) + hex6,
# but we accept old IDs with %H%M%S (6-digit) + hex8 still in the DB.
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-]*-\d{8}t\d{4,6}-[a-f0-9]{6,8}$")

REQUIRED_BACKTEST_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "run_metadata",
    "equity_curve",
    "order_events",
    "metrics",
)

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
    benchmark_equity: float | None = None
    benchmark_period_return: float | None = None
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    concentration: float = 0.0
    turnover: float = 0.0
    strategy: str | None = None


@dataclass(frozen=True)
class OrderEventRecord:
    """Single position lifecycle event for DB persistence."""

    event_id: str
    ts: datetime
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


@dataclass(frozen=True)
class PositionSnapshotPoint:
    """One open position's end-of-bar realized portfolio weight.

    ``market_value`` and ``realized_weight`` are signed: long exposure is
    positive and short exposure is negative.
    """

    ts: datetime
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
    symbol: str
    target_weight: float | None
    realized_weight: float
    weight_drift: float | None


@dataclass(frozen=True)
class StrategyMetrics:
    """Aggregate performance metrics for a backtest run.

    Units convention (not stored, documented here):
    - return fields: ratio (e.g. 0.05 = 5%)
    - max_drawdown: negative ratio (e.g. -0.15 = 15% decline)
    - win_rate/exposure_ratio: ratio 0-1
    - sharpe/sortino/calmar/profit_factor/payoff_ratio/information_ratio:
      dimensionless scores; None when their required denominator population
      is absent or zero
    - trades: count
    - avg_trade_return: notional-weighted mean net return per realized exit
    """

    # Universal (all strategies produce these)
    total_return: float
    max_drawdown: float = 0.0
    trades: int = 0

    # Annualized (None when disabled, the sample is insufficient, or not computable)
    annual_return: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None

    # Most strategies (None if not applicable)
    win_rate: float | None = None
    profit_factor: float | None = None
    payoff_ratio: float | None = None
    avg_trade_return: float | None = None
    exposure_ratio: float | None = None

    # Benchmark
    benchmark_return: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None

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
class BacktestOutput:
    """Top-level backtest output container.

    This is the canonical output object produced after a backtest run.
    Persist via db.timescale_writer.save_strategy_results().
    """

    run_metadata: RunMetadata
    equity_curve: Sequence[EquityCurvePoint]
    order_events: Sequence[OrderEventRecord]
    metrics: StrategyMetrics
    position_snapshots: Sequence[PositionSnapshotPoint]
    allocation_snapshots: Sequence[AllocationSnapshotPoint]

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
                f"'<strategy>-<symbol>[-<timeframe>]-<YYYYMMDDThhmm>-<hex6>', "
                f"got {self.run_metadata.run_id!r}"
            )
        if not self.run_metadata.strategy:
            raise ValueError("run_metadata.strategy is required")
        if not self.run_metadata.symbols or any(not symbol for symbol in self.run_metadata.symbols):
            raise ValueError("run_metadata.symbols must contain non-empty identifiers")
        if not self.run_metadata.timeframe:
            raise ValueError("run_metadata.timeframe is required")
        if self.equity_curve is None:
            raise ValueError("equity_curve is required (may be empty list)")


def ensure_snake_case_keys(keys: list[str] | tuple[str, ...], record_name: str) -> None:
    """Validate that all keys are snake_case."""
    invalid = [k for k in keys if not SNAKE_CASE_PATTERN.match(str(k))]
    if invalid:
        raise ValueError(f"{record_name} has non-snake_case keys: {invalid}")


def require_keys(record: dict[str, Any], keys: tuple[str, ...], record_name: str) -> None:
    """Validate that all required keys are present and non-empty."""
    missing = [k for k in keys if k not in record or record[k] in (None, "")]
    if missing:
        raise ValueError(f"{record_name} missing required keys: {missing}")
