"""Standardized backtest output schema.

All field names: strict snake_case.
Units are explicit fields; never encoded inside key names or values.
Cost/slippage fields are reserved (may be None for simple backtests).

Storage targets: JSON (Streamlit), CSV equity curve (Grafana/Streamlit).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional, Sequence

from librae.contracts import SCHEMA_VERSION

BACKTEST_SCHEMA_VERSION = SCHEMA_VERSION

VALID_SAMPLE_LABELS = frozenset({"train", "validation", "oos", "live"})

RUN_ID_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9_.\-]*-\d{8}t\d{6}-[a-f0-9]{8}$"
)


@dataclass(frozen=True)
class RunMetadata:
    """Identifies and describes a backtest run."""

    run_id: str
    strategy: str
    symbol: str
    timeframe: str
    start_ts: datetime
    end_ts: datetime
    run_ts: datetime
    data_source: str
    data_version: str
    schema_version: str = BACKTEST_SCHEMA_VERSION
    # Optional human label
    label: Optional[str] = None
    # Reserved: venue
    venue: Optional[str] = None
    # Sample split label — must be one of VALID_SAMPLE_LABELS when set
    sample: Optional[str] = None


@dataclass(frozen=True)
class EquityCurvePoint:
    """Single point on the equity curve."""

    ts: datetime
    equity: float
    equity_unit: str
    ret_1d: float
    drawdown: float
    benchmark_equity: Optional[float] = None
    benchmark_ret_1d: Optional[float] = None


@dataclass(frozen=True)
class TradeRecord:
    """Single completed trade."""

    trade_id: str
    entry_ts: datetime
    exit_ts: datetime
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    price_unit: str
    quantity_unit: str
    gross_pnl: float
    net_pnl: float
    pnl_unit: str
    # Reserved: cost breakdown
    commission: Optional[float] = None
    commission_unit: Optional[str] = None
    slippage: Optional[float] = None
    slippage_unit: Optional[str] = None
    # Reserved: bar counts
    holding_bars: Optional[int] = None


@dataclass
class StrategyMetrics:
    """Aggregate performance metrics for a backtest run.

    Units convention (not stored, documented here):
    - return/drawdown fields: ratio (e.g. 0.05 = 5%)
    - win_rate/exposure_ratio: ratio 0-1
    - sharpe/sortino/calmar/profit_factor: score (dimensionless)
    - trades: count
    - avg_pnl_points: quote currency points
    """

    # Universal (all strategies produce these)
    total_return: float
    annual_return: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_drawdown: float = 0.0
    trades: int = 0

    # Most strategies (None if not applicable)
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    avg_trade_return: Optional[float] = None
    avg_pnl_points: Optional[float] = None
    exposure_ratio: Optional[float] = None

    # Benchmark
    benchmark_return: Optional[float] = None

    # Cost breakdown
    total_commission: Optional[float] = None
    total_slippage: Optional[float] = None


@dataclass
class BacktestOutput:
    """Top-level backtest output container.

    This is the canonical output object produced after a backtest run.
    Persist via librae.persistence.
    """

    run_metadata: RunMetadata
    equity_curve: Sequence[EquityCurvePoint]
    trades: Sequence[TradeRecord]
    metrics: StrategyMetrics

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
                f"'<strategy>-<symbol>-<YYYYMMDDThhmmss>-<hex8>', "
                f"got {self.run_metadata.run_id!r}"
            )
        if not self.run_metadata.strategy:
            raise ValueError("run_metadata.strategy is required")
        if not self.run_metadata.symbol:
            raise ValueError("run_metadata.symbol is required")
        if not self.run_metadata.timeframe:
            raise ValueError("run_metadata.timeframe is required")
        if self.equity_curve is None:
            raise ValueError("equity_curve is required (may be empty list)")
        if self.trades is None:
            raise ValueError("trades is required (may be empty list)")
        sample = self.run_metadata.sample
        if sample is not None and sample not in VALID_SAMPLE_LABELS:
            raise ValueError(
                f"run_metadata.sample must be one of {sorted(VALID_SAMPLE_LABELS)}, got {sample!r}"
            )
