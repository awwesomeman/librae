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
from .core.executor import Executor, TradePnL, calc_trade_pnl, direction, make_fill
from .core.metrics import compute_all
from .core.utils import generate_run_id, make_trade_id, infer_timeframe, to_ccxt, to_canonical
from .utils import metrics_dict_to_backtest_output

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
    "metrics_dict_to_backtest_output",
    "CostModel",
    "Action",
    "BaseStrategy",
    "Context",
    "Fill",
    "Position",
    "Executor",
    "TradePnL",
    "calc_trade_pnl",
    "direction",
    "make_fill",
    "Backtest",
    "BacktestResult",
    "EquitySnapshot",
    "TradeResult",
    "compute_all",
    "generate_run_id",
    "make_trade_id",
    "infer_timeframe",
    "to_ccxt",
    "to_canonical",
]
