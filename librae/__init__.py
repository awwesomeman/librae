"""Backtest engine, strategy protocol, cost model, and metrics.

Re-exports from subpackages for convenience.
"""

from .backtest.schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
    RUN_ID_PATTERN,
)
from .backtest.engine import Backtest, BacktestResult, EquitySnapshot
from .core.cost_model import CostModel
from .core.strategy import Action, BaseStrategy, Context, Fill, Position
from .core.executor import (
    TradePnL, TradeResult, calc_trade_pnl, close_position, direction,
    make_fill, process_actions, scale_into_position, reduce_position,
)
from .core.metrics import compute_all
from .core.utils import generate_run_id, make_trade_id, infer_timeframe, to_ccxt, to_canonical
from .config.market_config import MarketConfig, get_market

__all__ = [
    "BacktestOutput",
    "EquityCurvePoint",
    "RunMetadata",
    "StrategyMetrics",
    "TradeRecord",
    "RUN_ID_PATTERN",
    "CostModel",
    "MarketConfig",
    "get_market",
    "Action",
    "BaseStrategy",
    "Context",
    "Fill",
    "Position",
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
