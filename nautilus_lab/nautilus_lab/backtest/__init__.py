"""Backtest output schema and persistence."""

from .schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
)
from .persistence import save_backtest_output, load_backtest_output

__all__ = [
    "BacktestOutput",
    "EquityCurvePoint",
    "RunMetadata",
    "StrategyMetrics",
    "TradeRecord",
    "save_backtest_output",
    "load_backtest_output",
]
