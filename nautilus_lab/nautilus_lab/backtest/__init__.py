"""Backtest output schema, persistence, and protocol runners."""

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
from .adapter import generate_run_id, metrics_dict_to_backtest_output
from .scoring import REQUIRED_METRICS_KEYS, score, validate_metrics
from .metrics import (
    MetricResult,
    register_metric,
    get_registry,
    compute_all,
    compute_one,
)
from .runners import (
    Periods,
    WFWindow,
    run_strict_protocol,
    run_walkforward,
    run_stability,
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
    "generate_run_id",
    "metrics_dict_to_backtest_output",
    "REQUIRED_METRICS_KEYS",
    "score",
    "validate_metrics",
    "Periods",
    "WFWindow",
    "run_strict_protocol",
    "run_walkforward",
    "run_stability",
    "MetricResult",
    "register_metric",
    "get_registry",
    "compute_all",
    "compute_one",
]
