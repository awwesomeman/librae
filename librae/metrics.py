"""Performance metrics module — QuantStats adapter + custom metrics.

Standard metrics (Sharpe, Sortino, MDD, Calmar, etc.) delegated to QuantStats.
Custom metrics not in QuantStats (exposure_ratio, avg_hold_bars) computed here.
compute_all() is the single entry point, takes engine.BacktestResult.

Quant best practices:
- ddof=1 consistent (sample std, industry standard)
- Annualization: 8760 hours/year for 24/7 crypto, 252 days for equity
- Sortino: full-sample denominator per skill spec
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Callable

import numpy as np
import pandas as pd
import quantstats as qs

from .schema import StrategyMetrics

if TYPE_CHECKING:
    from .engine import BacktestResult

logger = logging.getLogger(__name__)

EPSILON = 1e-9


def compute_all(
    result: BacktestResult,
    start_ts: datetime,
    end_ts: datetime,
) -> StrategyMetrics:
    """Compute all metrics from BacktestResult using QuantStats.

    Args:
        result: Raw output from engine.run_backtest().
        start_ts: Backtest start timestamp.
        end_ts: Backtest end timestamp.

    Returns:
        StrategyMetrics dataclass for BacktestOutput.
    """
    if not result.equity_curve:
        return StrategyMetrics(total_return=0.0, trades=0)

    equity_vals = np.array(
        [s.equity for s in result.equity_curve], dtype=np.float64,
    )
    returns = pd.Series(
        np.diff(equity_vals) / (equity_vals[:-1] + EPSILON),
        dtype=np.float64,
    )

    n_trades = len(result.trades)
    if n_trades == 0 or len(returns) < 2:
        total_ret = equity_vals[-1] / (equity_vals[0] + EPSILON) - 1.0
        return StrategyMetrics(total_return=float(total_ret), trades=n_trades)

    # QuantStats metrics
    sharpe = _safe_qs(qs.stats.sharpe, returns, periods=8760)
    sortino = _safe_qs(qs.stats.sortino, returns, periods=8760)
    max_dd = _safe_qs(qs.stats.max_drawdown, returns)
    calmar = _safe_qs(qs.stats.calmar, returns)

    # Single-pass trade field extraction
    net_pnls = np.empty(n_trades, dtype=np.float64)
    gross_pnls = np.empty(n_trades, dtype=np.float64)
    entry_prices = np.empty(n_trades, dtype=np.float64)
    holding_bars = np.empty(n_trades, dtype=np.int64)
    commissions = np.empty(n_trades, dtype=np.float64)
    slippages = np.empty(n_trades, dtype=np.float64)
    for i, t in enumerate(result.trades):
        net_pnls[i] = t.net_pnl
        gross_pnls[i] = t.gross_pnl
        entry_prices[i] = t.entry_price
        holding_bars[i] = t.holding_bars
        commissions[i] = t.commission
        slippages[i] = t.slippage

    wins = gross_pnls[gross_pnls > 0]
    losses_abs = np.abs(gross_pnls[gross_pnls <= 0])
    win_rate = float(len(wins) / n_trades) if n_trades > 0 else 0.0
    profit_factor = (
        float(wins.sum() / (losses_abs.sum() + EPSILON))
        if len(losses_abs) > 0 else 0.0
    )

    total_ret = float(equity_vals[-1] / (equity_vals[0] + EPSILON) - 1.0)
    span_seconds = (end_ts - start_ts).total_seconds()
    years = span_seconds / (365.25 * 86400) if span_seconds > 0 else 1.0
    ann_return = float((1 + total_ret) ** (1 / years) - 1) if years > 0 else 0.0

    trade_returns = net_pnls / (entry_prices + EPSILON)
    avg_trade_return = float(np.mean(trade_returns))
    avg_pnl_points = float(np.mean(net_pnls))

    total_bars = len(result.equity_curve)
    exposure_ratio = float(holding_bars.sum() / total_bars) if total_bars > 0 else 0.0

    total_commission = float(commissions.sum())
    total_slippage = float(slippages.sum())

    return StrategyMetrics(
        total_return=total_ret,
        annual_return=ann_return,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown=abs(max_dd),
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_trade_return=avg_trade_return,
        avg_pnl_points=avg_pnl_points,
        trades=n_trades,
        exposure_ratio=exposure_ratio,
        total_commission=total_commission,
        total_slippage=total_slippage,
    )


def _safe_qs(fn: Callable, returns: pd.Series, **kwargs) -> float:
    """Call a QuantStats function, return 0.0 on error."""
    try:
        val = fn(returns, **kwargs)
        if val is None or np.isnan(val) or np.isinf(val):
            return 0.0
        return float(val)
    except Exception:
        logger.warning("QuantStats %s failed, returning 0.0", fn.__name__)
        return 0.0
