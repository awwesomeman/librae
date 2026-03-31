"""Performance metrics module — QuantStats adapter + custom metrics.

Standard metrics (Sharpe, Sortino, MDD, Calmar, etc.) delegated to QuantStats.
Custom metrics not in QuantStats (exposure_ratio, avg_hold_bars) computed here.
compute_all() is the single entry point, takes engine.BacktestResult.

Annualization periods are inferred from the data's bar frequency (median time
delta between consecutive bars), so the same code works for H1, D1, W1, etc.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Callable

import numpy as np
import pandas as pd
import quantstats as qs

if TYPE_CHECKING:
    from librae.backtest.engine import BacktestResult
    from librae.backtest.schema import StrategyMetrics

from librae.core import EPSILON

logger = logging.getLogger(__name__)
SECONDS_PER_YEAR = 365.25 * 86400


def _infer_annual_periods(index: pd.DatetimeIndex) -> int:
    """Infer how many bars fit in one year from actual data density.

    Uses ``n_bars / span_years`` — the true observed bar rate — so it
    correctly handles markets with limited trading hours (e.g. 5h/day
    futures) and any bar frequency (H1, D1, W1, etc.).
    """
    if len(index) < 2:
        return 252  # safe fallback
    span_seconds = (index[-1] - index[0]).total_seconds()
    if span_seconds <= 0:
        return 252
    span_years = span_seconds / SECONDS_PER_YEAR
    return max(1, int(round(len(index) / span_years)))


def compute_all(
    result: BacktestResult,
    start_ts: datetime,
    end_ts: datetime,
    annualize: bool = True,
) -> StrategyMetrics:
    """Compute all metrics from BacktestResult using QuantStats.

    Args:
        result: Raw output from engine.run_backtest().
        start_ts: Backtest start timestamp.
        end_ts: Backtest end timestamp.
        annualize: If True, infer periods from data and compute
            annual_return/sharpe/sortino/calmar.  If False, these
            fields are None.

    Returns:
        StrategyMetrics dataclass for BacktestOutput.
    """
    # WHY: import here to avoid circular dependency (metrics → schema → metrics)
    from librae.backtest.schema import StrategyMetrics

    if not result.equity_curve:
        return StrategyMetrics(total_return=0.0, trades=0)

    equity_vals = np.array(
        [s.equity for s in result.equity_curve], dtype=np.float64,
    )
    # WHY: QuantStats max_drawdown/calmar require DatetimeIndex, not RangeIndex
    timestamps = pd.DatetimeIndex([s.ts for s in result.equity_curve[1:]])
    returns = pd.Series(
        np.diff(equity_vals) / (equity_vals[:-1] + EPSILON),
        index=timestamps,
        dtype=np.float64,
    )

    n_trades = len(result.trades)
    if n_trades == 0 or len(returns) < 2:
        total_ret = _safe_qs(qs.stats.comp, returns) if len(returns) > 0 else 0.0
        return StrategyMetrics(total_return=total_ret, trades=n_trades)

    max_dd = _safe_qs(qs.stats.max_drawdown, returns)

    # Annualized metrics (None when annualize=False)
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    ann_return: float | None = None
    if annualize:
        periods = _infer_annual_periods(timestamps)
        sharpe = _safe_qs(qs.stats.sharpe, returns, periods=periods)
        sortino = _safe_qs(qs.stats.sortino, returns, periods=periods)
        calmar = _safe_qs(qs.stats.calmar, returns)
        ann_return = _safe_qs(qs.stats.cagr, returns, periods=periods)

    # Single-pass trade field extraction
    net_pnls = np.empty(n_trades, dtype=np.float64)
    entry_prices = np.empty(n_trades, dtype=np.float64)
    holding_bars = np.empty(n_trades, dtype=np.int64)
    commissions = np.empty(n_trades, dtype=np.float64)
    slippages = np.empty(n_trades, dtype=np.float64)
    for i, t in enumerate(result.trades):
        net_pnls[i] = t.net_pnl
        entry_prices[i] = t.entry_price
        holding_bars[i] = t.holding_bars
        commissions[i] = t.commission
        slippages[i] = t.slippage

    # WHY: use net_pnl (after costs) for all trade-level metrics
    # to stay consistent with total_return which is also net-of-costs.
    wins = net_pnls[net_pnls > 0]
    losses_abs = np.abs(net_pnls[net_pnls <= 0])
    win_rate = float(len(wins) / n_trades) if n_trades > 0 else 0.0
    profit_factor = (
        float(wins.sum() / (losses_abs.sum() + EPSILON))
        if len(losses_abs) > 0 else 0.0
    )

    total_ret = _safe_qs(qs.stats.comp, returns)

    trade_returns = net_pnls / (entry_prices + EPSILON)
    avg_trade_return = float(np.mean(trade_returns))

    total_bars = len(result.equity_curve)
    exposure_ratio = float(holding_bars.sum() / total_bars) if total_bars > 0 else 0.0

    total_commission = float(commissions.sum())
    total_slippage = float(slippages.sum())

    # Benchmark return (if benchmark_curve available)
    benchmark_return: float | None = None
    if result.benchmark_curve and len(result.benchmark_curve) >= 2:
        bm = result.benchmark_curve
        benchmark_return = float(bm[-1] / (bm[0] + EPSILON) - 1.0)

    return StrategyMetrics(
        total_return=total_ret,
        annual_return=ann_return,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown=abs(max_dd),
        trades=n_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_trade_return=avg_trade_return,
        exposure_ratio=exposure_ratio,
        benchmark_return=benchmark_return,
        total_commission=total_commission,
        total_slippage=total_slippage,
    )


def _safe_qs(fn: Callable, returns: pd.Series, **kwargs: float) -> float:
    """Call a QuantStats function, return 0.0 on error."""
    try:
        val = fn(returns, **kwargs)
        if val is None or np.isnan(val) or np.isinf(val):
            return 0.0
        return float(val)
    except Exception:
        logger.warning("QuantStats %s failed, returning 0.0", fn.__name__)
        return 0.0
