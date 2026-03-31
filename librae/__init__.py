"""Backtest engine, strategy protocol, cost model, metrics, and persistence."""

from .schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
    VALID_SAMPLE_LABELS,
    RUN_ID_PATTERN,
)
from .persistence import save_backtest_output, load_backtest_output
from .utils import build_backtest_output, generate_run_id, metrics_dict_to_backtest_output
from .scoring import REQUIRED_METRICS_KEYS, score, validate_metrics
from .cost_model import CostModel
from .strategy import Action, BaseStrategy, Context, Fill, Position
from .executor import BacktestExecutor, Executor
from .engine import Backtest, BacktestResult, EquitySnapshot, TradeResult
from .metrics import compute_all
from .runners import (
    Periods,
    WFWindow,
    run_strict_protocol,
    run_walkforward,
    run_stability,
    make_backtest_fn,
)

__all__ = [
    "BacktestOutput",
    "EquityCurvePoint",
    "RunMetadata",
    "StrategyMetrics",
    "TradeRecord",
    "VALID_SAMPLE_LABELS",
    "RUN_ID_PATTERN",
    "save_backtest_output",
    "load_backtest_output",
    "build_backtest_output",
    "generate_run_id",
    "metrics_dict_to_backtest_output",
    "REQUIRED_METRICS_KEYS",
    "score",
    "validate_metrics",
    "CostModel",
    "Action",
    "BaseStrategy",
    "Context",
    "Fill",
    "Position",
    "BacktestExecutor",
    "Executor",
    "Backtest",
    "BacktestResult",
    "EquitySnapshot",
    "TradeResult",
    "compute_all",
    "Periods",
    "WFWindow",
    "run_strict_protocol",
    "run_walkforward",
    "run_stability",
    "make_backtest_fn",
]
