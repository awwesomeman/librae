from .trendpullback_mxfr1 import TrendPullBackParams as TrendPullBackParamsMXFR1
from .trendpullback_mxfr1 import run_trendpullback_backtest
from .trendpullback_btc import TrendPullBackParams, backtest, generate_signals, Signal

__all__ = [
    "TrendPullBackParamsMXFR1",
    "run_trendpullback_backtest",
    "TrendPullBackParams",
    "backtest",
    "generate_signals",
    "Signal",
]
