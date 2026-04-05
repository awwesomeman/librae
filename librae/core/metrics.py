"""Performance metrics module — QuantStats adapter + custom metrics.

Standard metrics (Sharpe, Sortino, MDD, Calmar, etc.) delegated to QuantStats.
Custom metrics not in QuantStats (exposure_ratio, avg_hold_bars) computed here.
compute_all() is the single entry point, accepts primitive sequences.

Annualization periods are inferred from the data's bar frequency (median time
delta between consecutive bars), so the same code works for H1, D1, W1, etc.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Sequence

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from librae.backtest.schema import StrategyMetrics
    from librae.core.executor import TradePnL

from librae.core import EPSILON

logger = logging.getLogger(__name__)

DEFAULT_ANNUAL_PERIODS = 252
SECONDS_PER_YEAR = 365.25 * 86400


def _infer_annual_periods(index: pd.DatetimeIndex) -> int:
    """Infer how many bars fit in one year from actual data density.

    Uses ``n_bars / span_years`` — the true observed bar rate — so it
    correctly handles markets with limited trading hours (e.g. 5h/day
    futures) and any bar frequency (H1, D1, W1, etc.).
    """
    if len(index) < 2:
        return DEFAULT_ANNUAL_PERIODS
    span_seconds = (index[-1] - index[0]).total_seconds()
    if span_seconds <= 0:
        return DEFAULT_ANNUAL_PERIODS
    span_years = span_seconds / SECONDS_PER_YEAR
    return max(1, int(round(len(index) / span_years)))


def compute_all(
    equity_values: Sequence[float],
    timestamps: Sequence[datetime],
    trade_pnls: Sequence[TradePnL],
    total_bars: int,
    annualize: bool = False,
    benchmark_values: Sequence[float] | None = None,
    exposed_bars: int | None = None,
    trade_quantities: Sequence[float] | None = None,
) -> StrategyMetrics:
    """Compute all metrics from equity curve + trades.

    Args:
        equity_values: Raw equity values per bar.
        timestamps: Corresponding timestamps (used for annualization).
        trade_pnls: TradePnL from core.executor.calc_trade_pnl().
        total_bars: Total bar count (for exposure_ratio).
        annualize: If True, compute annualized metrics.
        benchmark_values: Buy-and-hold equity values for benchmark comparison.
        exposed_bars: Number of bars with at least one open position.
        trade_quantities: Per-trade closed quantity (for quantity-weighted avg return).

    Called once by backtest (at build_output time).
    Called periodically by live (based on monitoring frequency).
    """
    # WHY: lazy imports — quantstats pulls in matplotlib/scipy (~1-3s),
    # deferred so `import librae` stays fast.
    import quantstats as qs
    from librae.backtest.schema import StrategyMetrics

    if not equity_values:
        return StrategyMetrics(total_return=0.0, trades=0)

    eq_arr = np.array(equity_values, dtype=np.float64)
    # WHY: QuantStats max_drawdown/calmar require DatetimeIndex, not RangeIndex
    ts_index = pd.DatetimeIndex(timestamps[1:]) if len(timestamps) > 1 else pd.DatetimeIndex([])
    returns = pd.Series(
        np.diff(eq_arr) / (eq_arr[:-1] + EPSILON),
        index=ts_index,
        dtype=np.float64,
    )

    n_trades = len(trade_pnls)
    if n_trades == 0 or len(returns) < 2:
        _comp = _safe_qs(qs.stats.comp, returns) if len(returns) > 0 else None
        total_ret = _comp if _comp is not None else 0.0
        return StrategyMetrics(total_return=total_ret, trades=n_trades)

    _dd = _safe_qs(qs.stats.max_drawdown, returns)
    max_dd = _dd if _dd is not None else 0.0

    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    ann_return: float | None = None
    if annualize:
        periods = _infer_annual_periods(ts_index)
        sharpe = _safe_qs(qs.stats.sharpe, returns, periods=periods)
        sortino = _safe_qs(qs.stats.sortino, returns, periods=periods)
        calmar = _safe_qs(qs.stats.calmar, returns)
        ann_return = _safe_qs(qs.stats.cagr, returns, periods=periods)

    # Trade-level metrics from TradePnL
    net_pnls = np.array([t.net_pnl for t in trade_pnls], dtype=np.float64)
    commissions = np.array([t.commission for t in trade_pnls], dtype=np.float64)
    slippages = np.array([t.slippage for t in trade_pnls], dtype=np.float64)
    taxes = np.array([t.tax for t in trade_pnls], dtype=np.float64)

    wins = net_pnls[net_pnls > 0]
    losses_abs = np.abs(net_pnls[net_pnls < 0])
    win_rate = float(len(wins) / n_trades) if n_trades > 0 else 0.0
    # WHY: profit_factor undefined when no losses (all wins) — return None,
    # not 0.0 which misleadingly suggests worst performance.
    profit_factor = (
        float(wins.sum() / (losses_abs.sum() + EPSILON))
        if len(losses_abs) > 0 else None
    )

    _comp = _safe_qs(qs.stats.comp, returns)
    total_ret = _comp if _comp is not None else 0.0
    # WHY: TradePnL.net_return is percentage (*100); convert to ratio
    # for consistency with other StrategyMetrics return fields.
    # Use quantity-weighted average when quantities are available
    # to correctly handle partial closes with different sizes.
    trade_returns = np.array([t.net_return for t in trade_pnls], dtype=np.float64)
    if trade_quantities is not None:
        if len(trade_quantities) != n_trades:
            raise ValueError(
                f"trade_quantities length ({len(trade_quantities)}) "
                f"must match trade_pnls length ({n_trades})"
            )
        qty_weights = np.array(trade_quantities, dtype=np.float64)
        avg_trade_return = float(np.average(trade_returns, weights=qty_weights)) / 100.0
    else:
        avg_trade_return = float(np.mean(trade_returns)) / 100.0

    exposure_ratio = 0.0
    if exposed_bars is not None and total_bars > 0:
        exposure_ratio = float(exposed_bars / total_bars)

    benchmark_return: float | None = None
    if benchmark_values and len(benchmark_values) >= 2:
        benchmark_return = float(benchmark_values[-1] / (benchmark_values[0] + EPSILON) - 1.0)

    return StrategyMetrics(
        total_return=total_ret,
        annual_return=ann_return,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown=max_dd,
        trades=n_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_trade_return=avg_trade_return,
        exposure_ratio=exposure_ratio,
        benchmark_return=benchmark_return,
        total_commission=float(commissions.sum()),
        total_slippage=float(slippages.sum()),
        total_tax=float(taxes.sum()),
    )


def _safe_qs(fn: Callable, returns: pd.Series, **kwargs: int | float) -> float | None:
    """Call a QuantStats function, return None if not computable."""
    try:
        val = fn(returns, **kwargs)
        if val is None or np.isnan(val) or np.isinf(val):
            return None
        return float(val)
    except Exception:
        logger.warning("QuantStats %s failed, returning None", fn.__name__)
        return None
