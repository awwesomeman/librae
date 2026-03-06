"""Standardized backtest output schema.

All field names: strict snake_case.
Units are explicit fields; never encoded inside key names or values.
Cost/slippage fields are reserved (may be None for simple backtests).

Storage targets: JSON (Streamlit), CSV equity curve (Grafana/Streamlit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from nautilus_lab.contracts import SCHEMA_VERSION

BACKTEST_SCHEMA_VERSION = SCHEMA_VERSION


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
    # Reserved: venue/sample split
    venue: Optional[str] = None
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
    """Aggregate performance metrics for a backtest run."""

    total_return: float
    total_return_unit: str = "ratio"
    annual_return: float = 0.0
    annual_return_unit: str = "ratio"
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_unit: str = "ratio"
    win_rate: float = 0.0
    win_rate_unit: str = "ratio_0_1"
    profit_factor: float = 0.0
    avg_trade_return: float = 0.0
    avg_trade_return_unit: str = "ratio"
    trades: int = 0
    trades_unit: str = "count"
    exposure_ratio: float = 0.0
    exposure_ratio_unit: str = "ratio_0_1"
    bh_total_return: float = 0.0
    bh_total_return_unit: str = "ratio"
    # Reserved: cost/slippage totals
    total_commission: Optional[float] = None
    total_commission_unit: Optional[str] = None
    total_slippage: Optional[float] = None
    total_slippage_unit: Optional[str] = None


@dataclass
class BacktestOutput:
    """Top-level backtest output container.

    This is the canonical output object produced after a backtest run.
    Persist via nautilus_lab.backtest.persistence.
    """

    run_metadata: RunMetadata
    equity_curve: Sequence[EquityCurvePoint]
    trades: Sequence[TradeRecord]
    metrics: StrategyMetrics

    def validate(self) -> None:
        """Raise ValueError if required fields are empty/missing."""
        if not self.run_metadata.run_id:
            raise ValueError("run_metadata.run_id is required")
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
