"""Pluggable performance metrics module.

Architecture:
- Each metric is a plain function decorated with @register_metric.
- The registry maps metric names to callables.
- compute_all() runs all registered metrics on a BacktestOutput.
- To add a new metric: define a function and decorate it. No core changes needed.

Metric function signature:
    def my_metric(output: BacktestOutput) -> float

The decorator wraps the return value into a MetricResult with name and unit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from .schema import BacktestOutput, TradeRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricResult:
    """Single computed metric value."""
    name: str
    value: float
    unit: str


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

RawMetricFn = Callable[[BacktestOutput], float]
MetricFn = Callable[[BacktestOutput], MetricResult]

_METRIC_REGISTRY: dict[str, MetricFn] = {}


def register_metric(name: str, unit: str) -> Callable[[RawMetricFn], MetricFn]:
    """Decorator to register a metric function.

    The decorated function only needs to return a float.
    The decorator wraps it to return MetricResult(name, value, unit).
    """
    def decorator(fn: RawMetricFn) -> MetricFn:
        def wrapper(output: BacktestOutput) -> MetricResult:
            value = fn(output)
            return MetricResult(name=name, value=value, unit=unit)
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        _METRIC_REGISTRY[name] = wrapper
        return wrapper
    return decorator


def get_registry() -> dict[str, MetricFn]:
    """Return a copy of the current metric registry."""
    return dict(_METRIC_REGISTRY)


def compute_all(output: BacktestOutput) -> dict[str, MetricResult]:
    """Compute all registered metrics for a BacktestOutput.

    Error isolation: if a single metric raises, it is logged and skipped.
    """
    results: dict[str, MetricResult] = {}
    for name, fn in _METRIC_REGISTRY.items():
        try:
            results[name] = fn(output)
        except Exception:
            logger.exception("metric %r failed, skipping", name)
    return results


def compute_one(name: str, output: BacktestOutput) -> MetricResult:
    """Compute a single named metric."""
    if name not in _METRIC_REGISTRY:
        raise KeyError(f"Unknown metric: {name!r}. Available: {sorted(_METRIC_REGISTRY)}")
    return _METRIC_REGISTRY[name](output)


# ---------------------------------------------------------------------------
# Vectorized helpers (shared across metrics)
# ---------------------------------------------------------------------------


def _trade_returns(trades: Sequence[TradeRecord]) -> np.ndarray:
    """Side-aware percentage returns per trade, vectorized."""
    if not trades:
        return np.array([], dtype=np.float64)
    entry = np.array([t.entry_price for t in trades], dtype=np.float64)
    exit_ = np.array([t.exit_price for t in trades], dtype=np.float64)
    sides = np.array([1.0 if t.side == "buy" else -1.0 for t in trades])
    return sides * (exit_ - entry) / np.where(entry != 0, entry, 1.0)


def _trade_pnl_points(trades: Sequence[TradeRecord]) -> np.ndarray:
    """Side-aware PnL in price points per trade, vectorized."""
    if not trades:
        return np.array([], dtype=np.float64)
    entry = np.array([t.entry_price for t in trades], dtype=np.float64)
    exit_ = np.array([t.exit_price for t in trades], dtype=np.float64)
    sides = np.array([1.0 if t.side == "buy" else -1.0 for t in trades])
    return sides * (exit_ - entry)


def _equity_array(output: BacktestOutput) -> np.ndarray | None:
    """Extract equity array from equity curve or synthesize from trades."""
    if output.equity_curve:
        return np.array([pt.equity for pt in output.equity_curve], dtype=np.float64)
    if output.trades:
        rets = _trade_returns(output.trades)
        return np.cumprod(np.concatenate([[1.0], 1.0 + rets]))
    return None


def _backtest_years(output: BacktestOutput) -> float:
    """Duration of the backtest in years."""
    dt = (output.run_metadata.end_ts - output.run_metadata.start_ts).total_seconds()
    return dt / (365.25 * 24 * 3600)


# ---------------------------------------------------------------------------
# Built-in metrics
# ---------------------------------------------------------------------------


@register_metric("sharpe", "score")
def metric_sharpe(output: BacktestOutput) -> float:
    """Annualized Sharpe ratio from trade returns (side-aware)."""
    rets = _trade_returns(output.trades)
    if len(rets) < 2:
        return 0.0

    mean_ret = float(np.mean(rets))
    std_ret = float(np.std(rets, ddof=1))
    if std_ret == 0:
        return 0.0

    years = _backtest_years(output)
    trades_per_year = len(rets) / years if years > 0 else len(rets)
    return float((mean_ret / std_ret) * np.sqrt(trades_per_year))


@register_metric("sortino", "score")
def metric_sortino(output: BacktestOutput) -> float:
    """Annualized Sortino ratio (downside deviation only)."""
    rets = _trade_returns(output.trades)
    if len(rets) < 2:
        return 0.0

    mean_ret = float(np.mean(rets))
    downside = rets[rets < 0]
    if len(downside) == 0:
        return float("inf") if mean_ret > 0 else 0.0
    downside_std = float(np.std(downside, ddof=1))
    if downside_std == 0:
        return 0.0

    years = _backtest_years(output)
    trades_per_year = len(rets) / years if years > 0 else len(rets)
    return float((mean_ret / downside_std) * np.sqrt(trades_per_year))


@register_metric("max_drawdown", "ratio")
def metric_max_drawdown(output: BacktestOutput) -> float:
    """Maximum drawdown from equity curve or trade sequence."""
    equity = _equity_array(output)
    if equity is None or len(equity) == 0:
        return 0.0

    peak = np.maximum.accumulate(equity)
    drawdowns = (peak - equity) / np.where(peak > 0, peak, 1.0)
    return float(np.max(drawdowns))


@register_metric("calmar", "score")
def metric_calmar(output: BacktestOutput) -> float:
    """Calmar ratio: annualized return / max drawdown."""
    equity = _equity_array(output)
    if equity is None or len(equity) < 2:
        return 0.0

    total_ret = equity[-1] / equity[0] - 1.0
    years = _backtest_years(output)
    if years <= 0:
        return 0.0
    ann_ret = (1 + total_ret) ** (1 / years) - 1

    # Compute MDD directly (not via registry wrapper which returns MetricResult)
    peak = np.maximum.accumulate(equity)
    drawdowns = (peak - equity) / np.where(peak > 0, peak, 1.0)
    mdd = float(np.max(drawdowns))

    if mdd == 0:
        return float("inf") if ann_ret > 0 else 0.0
    return ann_ret / mdd


@register_metric("profit_factor", "ratio")
def metric_profit_factor(output: BacktestOutput) -> float:
    """Gross profit / gross loss."""
    if not output.trades:
        return 0.0

    pnl = np.array([t.gross_pnl for t in output.trades], dtype=np.float64)
    gross_profit = float(np.sum(pnl[pnl > 0]))
    gross_loss = float(np.abs(np.sum(pnl[pnl <= 0])))

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


@register_metric("win_rate", "ratio_0_1")
def metric_win_rate(output: BacktestOutput) -> float:
    """Fraction of trades with positive gross PnL."""
    if not output.trades:
        return 0.0

    pnl = np.array([t.gross_pnl for t in output.trades], dtype=np.float64)
    return float(np.sum(pnl > 0) / len(pnl))


@register_metric("avg_pnl_points", "points")
def metric_avg_pnl_points(output: BacktestOutput) -> float:
    """Average PnL in price points per trade (side-aware)."""
    pts = _trade_pnl_points(output.trades)
    if len(pts) == 0:
        return 0.0
    return float(np.mean(pts))


@register_metric("total_return", "ratio")
def metric_total_return(output: BacktestOutput) -> float:
    """Total return from equity curve or compounded trade returns."""
    equity = _equity_array(output)
    if equity is None or len(equity) < 2:
        return 0.0
    return float(equity[-1] / equity[0] - 1.0)


@register_metric("annual_return", "ratio")
def metric_annual_return(output: BacktestOutput) -> float:
    """Annualized return."""
    equity = _equity_array(output)
    if equity is None or len(equity) < 2:
        return 0.0

    total_ret = equity[-1] / equity[0] - 1.0
    years = _backtest_years(output)
    if years <= 0:
        return 0.0
    return float((1 + total_ret) ** (1 / years) - 1)


@register_metric("payoff_ratio", "ratio")
def metric_payoff_ratio(output: BacktestOutput) -> float:
    """Average win / average loss (absolute)."""
    if not output.trades:
        return 0.0

    pnl = np.array([t.gross_pnl for t in output.trades], dtype=np.float64)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    if len(wins) == 0 or len(losses) == 0:
        return 0.0
    return float(np.mean(wins) / np.abs(np.mean(losses)))


@register_metric("expectancy", "currency")
def metric_expectancy(output: BacktestOutput) -> float:
    """Expected value per trade: avg_win * win_rate - avg_loss * loss_rate."""
    if not output.trades:
        return 0.0

    pnl = np.array([t.gross_pnl for t in output.trades], dtype=np.float64)
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    n = len(pnl)

    avg_win = float(np.mean(wins)) if len(wins) > 0 else 0.0
    avg_loss = float(np.abs(np.mean(losses))) if len(losses) > 0 else 0.0
    win_rate = len(wins) / n
    loss_rate = len(losses) / n

    return avg_win * win_rate - avg_loss * loss_rate
