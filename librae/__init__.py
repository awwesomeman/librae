"""Backtest engine, strategy protocol, cost model, metrics, and persistence.

Re-exports from subpackages for convenience.
"""

from .backtest.schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
    VALID_SAMPLE_LABELS,
    RUN_ID_PATTERN,
    SCHEMA_VERSION,
)
from .backtest.persistence import save_backtest_output, load_backtest_output
from .backtest.engine import Backtest, BacktestResult, EquitySnapshot, TradeResult
from .core.cost_model import CostModel
from .core.strategy import Action, BaseStrategy, Context, Fill, Position
from .core.executor import Executor, make_fill
from .core.metrics import compute_all
from .core.utils import generate_run_id

# WHY: backward compat — build_backtest_output is still used by strategy run.py
# until Part B converts to Backtest.build_output()
from .utils import build_backtest_output, metrics_dict_to_backtest_output

__all__ = [
    "BacktestOutput",
    "EquityCurvePoint",
    "RunMetadata",
    "StrategyMetrics",
    "TradeRecord",
    "VALID_SAMPLE_LABELS",
    "RUN_ID_PATTERN",
    "SCHEMA_VERSION",
    "save_backtest_output",
    "load_backtest_output",
    "build_backtest_output",
    "generate_run_id",
    "metrics_dict_to_backtest_output",
    "CostModel",
    "Action",
    "BaseStrategy",
    "Context",
    "Fill",
    "Position",
    "Executor",
    "make_fill",
    "Backtest",
    "BacktestResult",
    "EquitySnapshot",
    "TradeResult",
    "compute_all",
]
