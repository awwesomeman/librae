"""Performance metrics module — QuantStats adapter + custom metrics.

Standard metrics (Sharpe, Sortino, MDD, Calmar, etc.) delegated to QuantStats,
which always uses ddof=1 internally (not configurable — verified against
quantstats.stats.sharpe source, so this project doesn't expose a fake ddof knob).
Custom metrics not in QuantStats (exposure_ratio, avg_hold_periods) and
on-demand trade/signal outcome analysis are computed here. Public functions
accept primitive sequences and DataFrames rather than database dependencies.

The annualization factor fed to QuantStats is always inferred from actual bar
density (see _infer_annual_periods) so intraday timeframes annualize correctly.
annual_periods (trading days/year, e.g. 365 for crypto, 252 for TW) is only a
fallback for when density can't be inferred (<2 bars).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from matplotlib.axes import Axes

    from librae.backtest.schema import BacktestOutput, OrderEventRecord, StrategyMetrics
    from librae.core.executor import TradePnL

from librae.core import EPSILON

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 365.25 * 86400
PERCENTAGE_POINTS_PER_FRACTION = 100.0

SignalDirection = Literal["long", "short"]
SignalPriceColumn = Literal["open", "high", "low", "close"]
LifecycleStatus = Literal["complete", "incomplete"]


@dataclass(frozen=True)
class _PositionLifecycle:
    """One symbol's ordered position events from flat to flat or end-of-sample."""

    symbol: str
    ordinal: int
    start_sequence: int
    side: SignalDirection
    events: tuple[OrderEventRecord, ...]
    status: LifecycleStatus


@dataclass
class _LifecycleState:
    """Mutable reconstruction state; converted to a frozen lifecycle on completion."""

    ordinal: int
    start_sequence: int
    side: SignalDirection
    quantity: float
    entry_price: float
    events: list[OrderEventRecord]


def _infer_annual_periods(index: pd.DatetimeIndex, fallback: int) -> int:
    """Infer how many bars fit in one year from actual data density.

    Uses ``(n_bars - 1) / span_years`` — the observed interval rate — so it
    correctly handles markets with limited trading hours (e.g. 5h/day
    futures) and any bar frequency (H1, D1, W1, etc.). Falls back to
    ``fallback`` (the configured trading-days/year) when there isn't
    enough data to infer a rate from.
    """
    if len(index) < 2:
        return fallback
    span_seconds = (index[-1] - index[0]).total_seconds()
    if span_seconds <= 0:
        return fallback
    span_years = span_seconds / SECONDS_PER_YEAR
    return max(1, round((len(index) - 1) / span_years))


def _as_positive_finite_array(values: Sequence[float], name: str) -> np.ndarray:
    """Return a float64 value curve whose observations can safely be denominators."""
    array = np.asarray(values, dtype=np.float64)
    if not np.isfinite(array).all() or np.any(array <= 0):
        raise ValueError(f"{name} must be finite and positive")
    return array


def compute_all(
    equity_values: Sequence[float],
    timestamps: Sequence[datetime],
    trade_pnls: Sequence[TradePnL],
    total_periods: int,
    annualize: bool = False,
    benchmark_values: Sequence[float] | None = None,
    exposed_periods: int | None = None,
    trade_quantities: Sequence[float] | None = None,
    trade_notionals: Sequence[float] | None = None,
    turnover_values: Sequence[float] | None = None,
    gross_exposure_values: Sequence[float] | None = None,
    net_exposure_values: Sequence[float] | None = None,
    concentration_values: Sequence[float] | None = None,
    risk_free_rate: float = 0.0,
    annual_periods: int = 365,
) -> StrategyMetrics:
    """Compute all metrics from equity curve + trades.

    Args:
        equity_values: Finite, strictly positive equity values per bar.
        timestamps: Corresponding timestamps (used for annualization).
        trade_pnls: TradePnL from core.executor.calc_trade_pnl().
        total_periods: Total bar count (for exposure_ratio).
        annualize: If True, compute annualized metrics.
        benchmark_values: Finite, strictly positive buy-and-hold equity values
            aligned one-to-one with equity_values.
        exposed_periods: Number of bars with at least one open position.
        trade_quantities: Finite, positive per-trade closed quantity (for
            quantity-weighted avg return).
        trade_notionals: Per-trade absolute notional weight. Preferred over quantity for
            weighting returns across instruments with different prices/multipliers.
        turnover_values: Per-event absolute traded notional divided by equity.
        gross_exposure_values: Per-event sum of absolute realized weights.
        net_exposure_values: Per-event sum of signed realized weights.
        concentration_values: Per-event largest absolute realized weight.
        risk_free_rate: Annual risk-free rate (crypto=0, TW=0.015).
        annual_periods: Trading days per year (crypto=365, TW=252). Used only
            as a fallback when bar density can't be inferred from timestamps
            (<2 bars) — otherwise the real annualization factor is inferred
            from actual bar density, see _infer_annual_periods.

    Called once by backtest (at build_output time).
    Called periodically by live (based on monitoring frequency).
    """
    # WHY: lazy imports — quantstats pulls in matplotlib/scipy (~1-3s),
    # deferred so `import librae` stays fast.
    import quantstats as qs

    from librae.backtest.schema import StrategyMetrics

    if len(equity_values) == 0:
        return StrategyMetrics(total_return=0.0, trades=0)
    if len(timestamps) != len(equity_values):
        raise ValueError("timestamps length must match equity_values")
    if not np.isfinite(risk_free_rate):
        raise ValueError("risk_free_rate must be finite")
    if annual_periods <= 0:
        raise ValueError("annual_periods must be positive")

    eq_arr = _as_positive_finite_array(equity_values, "equity_values")

    benchmark_array: np.ndarray | None = None
    if benchmark_values is not None:
        if len(benchmark_values) != len(equity_values):
            raise ValueError("benchmark_values length must match equity_values")
        benchmark_array = _as_positive_finite_array(benchmark_values, "benchmark_values")

    # WHY: QuantStats max_drawdown/calmar require DatetimeIndex, not RangeIndex
    ts_index = pd.DatetimeIndex(timestamps[1:]) if len(timestamps) > 1 else pd.DatetimeIndex([])
    returns = pd.Series(
        np.diff(eq_arr) / eq_arr[:-1],
        index=ts_index,
        dtype=np.float64,
    )

    _comp = _safe_qs(qs.stats.comp, returns) if len(returns) > 0 else None
    total_ret = _comp if _comp is not None else 0.0
    _dd = _safe_qs(qs.stats.max_drawdown, returns) if len(returns) > 0 else None
    max_dd = _dd if _dd is not None else 0.0

    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    ann_return: float | None = None
    if annualize and len(returns) >= 2:
        # WHY: QuantStats expects bars-per-year, not trading-days-per-year,
        # so annualization always uses actual bar density (correct for any
        # timeframe — H1, D1, ...); annual_periods is only the fallback for
        # when density can't be inferred.
        periods = _infer_annual_periods(ts_index, fallback=annual_periods)

        # QuantStats accepts an annual risk-free rate and deannualizes it
        # internally using periods. Passing a per-bar rate would apply that
        # conversion twice.
        return_std = float(returns.std(ddof=1))
        if np.isfinite(return_std) and return_std > 0.0:
            sharpe = _safe_qs(qs.stats.sharpe, returns, periods=periods, rf=risk_free_rate)

        periodic_rf = (
            float(np.power(1.0 + risk_free_rate, 1.0 / periods) - 1.0)
            if risk_free_rate > 0.0
            else 0.0
        )
        if bool((returns < periodic_rf).any()):
            sortino = _safe_qs(qs.stats.sortino, returns, periods=periods, rf=risk_free_rate)

        # Calmar is undefined without a drawdown. Guard the denominator here
        # because QuantStats divides by zero before _safe_qs can map inf to None.
        calmar = _safe_qs(qs.stats.calmar, returns, periods=periods) if max_dd < 0.0 else None
        ann_return = _safe_qs(qs.stats.cagr, returns, periods=periods)

    n_trades = len(trade_pnls)
    net_pnls = np.array([t.net_pnl for t in trade_pnls], dtype=np.float64)
    commissions = np.array([t.commission for t in trade_pnls], dtype=np.float64)
    slippages = np.array([t.slippage for t in trade_pnls], dtype=np.float64)
    taxes = np.array([t.tax for t in trade_pnls], dtype=np.float64)

    wins = net_pnls[net_pnls > 0]
    losses_abs = np.abs(net_pnls[net_pnls < 0])
    win_rate = float(len(wins) / n_trades) if n_trades > 0 else None
    # WHY: profit_factor undefined when no losses (all wins) — return None,
    # not 0.0 which misleadingly suggests worst performance.
    profit_factor = float(wins.sum() / losses_abs.sum()) if len(losses_abs) > 0 else None
    # WHY: payoff_ratio (avg win / avg loss) is undefined without both sides present.
    payoff_ratio = (
        float(wins.mean() / losses_abs.mean()) if len(wins) > 0 and len(losses_abs) > 0 else None
    )

    # WHY: TradePnL.net_return is percentage (*100); convert to ratio
    # for consistency with other StrategyMetrics return fields.
    # Prefer notional weights across instruments; quantity remains a
    # backward-compatible fallback for single-instrument partial closes.
    trade_returns = np.array([t.net_return for t in trade_pnls], dtype=np.float64)
    avg_trade_return: float | None = None
    qty_weights: np.ndarray | None = None
    if trade_quantities is not None and len(trade_quantities) != n_trades:
        raise ValueError(
            f"trade_quantities length ({len(trade_quantities)}) "
            f"must match trade_pnls length ({n_trades})"
        )
    if trade_quantities is not None:
        qty_weights = np.asarray(trade_quantities, dtype=np.float64)
        if not np.isfinite(qty_weights).all() or np.any(qty_weights <= 0):
            raise ValueError("trade_quantities must be finite and positive")

    notional_weights: np.ndarray | None = None
    if trade_notionals is not None and len(trade_notionals) != n_trades:
        raise ValueError(
            f"trade_notionals length ({len(trade_notionals)}) "
            f"must match trade_pnls length ({n_trades})"
        )
    if trade_notionals is not None:
        notional_weights = np.asarray(trade_notionals, dtype=np.float64)
        if not np.isfinite(notional_weights).all() or np.any(notional_weights <= 0):
            raise ValueError("trade_notionals must be finite and positive")

    if n_trades > 0 and notional_weights is not None:
        avg_trade_return = float(np.average(trade_returns, weights=notional_weights)) / 100.0
    elif n_trades > 0 and qty_weights is not None:
        avg_trade_return = float(np.average(trade_returns, weights=qty_weights)) / 100.0
    elif n_trades > 0:
        avg_trade_return = float(np.mean(trade_returns)) / 100.0

    exposure_ratio: float | None = None
    if exposed_periods is not None and total_periods > 0:
        exposure_ratio = float(exposed_periods / total_periods)

    benchmark_return: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    if benchmark_array is not None and len(benchmark_array) >= 2:
        benchmark_return = float(benchmark_array[-1] / benchmark_array[0] - 1.0)
        benchmark_returns = np.diff(benchmark_array) / benchmark_array[:-1]
        active_returns = returns.to_numpy() - benchmark_returns
        if len(active_returns) >= 2:
            periods = _infer_annual_periods(ts_index, fallback=annual_periods)
            active_std = float(np.std(active_returns, ddof=1))
            tracking_error = active_std * np.sqrt(periods)
            if active_std > 0.0:
                information_ratio = float(np.mean(active_returns)) / active_std * np.sqrt(periods)

    total_turnover = _sum_optional(turnover_values)
    average_gross_exposure = _mean_optional(gross_exposure_values)
    max_gross_exposure = _max_optional(gross_exposure_values)
    max_abs_net_exposure = _max_optional(
        [abs(value) for value in net_exposure_values] if net_exposure_values is not None else None
    )
    max_concentration = _max_optional(concentration_values)

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
        payoff_ratio=payoff_ratio,
        avg_trade_return=avg_trade_return,
        exposure_ratio=exposure_ratio,
        benchmark_return=benchmark_return,
        tracking_error=tracking_error,
        information_ratio=information_ratio,
        total_turnover=total_turnover,
        average_gross_exposure=average_gross_exposure,
        max_gross_exposure=max_gross_exposure,
        max_abs_net_exposure=max_abs_net_exposure,
        max_concentration=max_concentration,
        total_commission=float(commissions.sum()),
        total_slippage=float(slippages.sum()),
        total_tax=float(taxes.sum()),
    )


def _sum_optional(values: Sequence[float] | None) -> float | None:
    return float(np.sum(values)) if values is not None else None


def _mean_optional(values: Sequence[float] | None) -> float | None:
    return float(np.mean(values)) if values is not None and len(values) > 0 else None


def _max_optional(values: Sequence[float] | None) -> float | None:
    return float(np.max(values)) if values is not None and len(values) > 0 else None


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


def generate_tearsheet(
    equity_values: Sequence[float],
    timestamps: Sequence[datetime],
    output_path: str = "tearsheet.html",
    title: str = "Strategy Performance Report",
    benchmark_values: Sequence[float] | None = None,
) -> str:
    """Generate QuantStats HTML tearsheet report with interactive plots and tables."""
    import quantstats as qs

    if len(equity_values) < 2:
        logger.warning("Not enough equity curve points to generate tearsheet")
        return ""
    if len(timestamps) != len(equity_values):
        raise ValueError("timestamps length must match equity_values")

    eq_arr = _as_positive_finite_array(equity_values, "equity_values")
    ts_index = pd.DatetimeIndex(timestamps[1:])
    returns = pd.Series(
        np.diff(eq_arr) / eq_arr[:-1],
        index=ts_index,
        dtype=np.float64,
    )

    benchmark_returns = None
    if benchmark_values is not None:
        if len(benchmark_values) != len(equity_values):
            raise ValueError("benchmark_values length must match equity_values")
        b_arr = _as_positive_finite_array(benchmark_values, "benchmark_values")
        benchmark_returns = pd.Series(
            np.diff(b_arr) / b_arr[:-1],
            index=ts_index,
            dtype=np.float64,
        )

    qs.reports.html(returns, benchmark=benchmark_returns, output=output_path, title=title)
    return output_path


def _lifecycle_error(event: OrderEventRecord, message: str) -> ValueError:
    return ValueError(f"{event.symbol} event {event.event_id}: {message}")


def _validate_event_values(event: OrderEventRecord) -> pd.Timestamp:
    if not event.event_id:
        raise ValueError("order event IDs must be non-empty")
    if not event.symbol:
        raise ValueError(f"event {event.event_id}: symbol must be non-empty")
    if event.event_type not in {"open", "add", "reduce", "close"}:
        raise _lifecycle_error(event, f"unsupported event_type {event.event_type!r}")
    _validate_direction(event.side)

    positive = {
        "fill_quantity": event.fill_quantity,
        "price": event.price,
        "entry_price": event.entry_price,
        "notional": event.notional,
    }
    for name, value in positive.items():
        if not np.isfinite(value) or value <= 0:
            raise _lifecycle_error(event, f"{name} must be finite and positive")
    if not np.isfinite(event.remaining_quantity) or event.remaining_quantity < 0:
        raise _lifecycle_error(event, "remaining_quantity must be finite and non-negative")
    if event.event_type in {"reduce", "close"} and (
        event.pnl is None or not np.isfinite(event.pnl)
    ):
        raise _lifecycle_error(event, "realized exit events require finite pnl")

    timestamp = pd.Timestamp(event.ts)
    if pd.isna(timestamp):
        raise _lifecycle_error(event, "timestamp must not be NaT")
    if timestamp.tzinfo is None:
        raise _lifecycle_error(event, "timestamp must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _quantities_match(left: float, right: float) -> bool:
    return bool(np.isclose(left, right, rtol=1e-9, atol=EPSILON))


def _reconstruct_position_lifecycles(
    order_events: Sequence[OrderEventRecord],
) -> list[_PositionLifecycle]:
    """Validate events and reconstruct independent per-symbol 0 -> N -> 0 lifecycles."""
    active: dict[str, _LifecycleState] = {}
    ordinals: dict[str, int] = {}
    last_timestamp: dict[str, pd.Timestamp] = {}
    seen_event_ids: set[str] = set()
    lifecycles: list[_PositionLifecycle] = []

    for sequence, event in enumerate(order_events):
        timestamp = _validate_event_values(event)
        if event.event_id in seen_event_ids:
            raise _lifecycle_error(event, "event_id must be unique")
        seen_event_ids.add(event.event_id)

        previous_timestamp = last_timestamp.get(event.symbol)
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise _lifecycle_error(event, "timestamps must not move backwards within a symbol")
        last_timestamp[event.symbol] = timestamp

        state = active.get(event.symbol)
        if event.event_type == "open":
            if state is not None:
                raise _lifecycle_error(event, "open requires a flat position")
            if not _quantities_match(event.remaining_quantity, event.fill_quantity):
                raise _lifecycle_error(event, "open remaining_quantity must equal fill_quantity")
            if not _quantities_match(event.entry_price, event.price):
                raise _lifecycle_error(event, "open entry_price must equal fill price")
            ordinal = ordinals.get(event.symbol, 0) + 1
            ordinals[event.symbol] = ordinal
            active[event.symbol] = _LifecycleState(
                ordinal=ordinal,
                start_sequence=sequence,
                side=event.side,
                quantity=event.remaining_quantity,
                entry_price=event.entry_price,
                events=[event],
            )
            continue

        if state is None:
            raise _lifecycle_error(event, f"{event.event_type} requires an active position")
        if event.side != state.side:
            raise _lifecycle_error(event, "side cannot change within a lifecycle")

        if event.event_type == "add":
            expected_quantity = state.quantity + event.fill_quantity
            expected_basis = (
                state.entry_price * state.quantity + event.price * event.fill_quantity
            ) / expected_quantity
            if not _quantities_match(event.remaining_quantity, expected_quantity):
                raise _lifecycle_error(event, "add remaining_quantity is inconsistent")
            if not _quantities_match(event.entry_price, expected_basis):
                raise _lifecycle_error(event, "add entry_price is not the weighted-average basis")
            state.quantity = event.remaining_quantity
            state.entry_price = event.entry_price
            state.events.append(event)
            continue

        expected_quantity = state.quantity - event.fill_quantity
        if not _quantities_match(event.entry_price, state.entry_price):
            raise _lifecycle_error(event, f"{event.event_type} must preserve entry_price")
        if event.event_type == "reduce":
            if expected_quantity <= EPSILON:
                raise _lifecycle_error(event, "reduce must leave a positive position")
            if not _quantities_match(event.remaining_quantity, expected_quantity):
                raise _lifecycle_error(event, "reduce remaining_quantity is inconsistent")
            state.quantity = event.remaining_quantity
            state.events.append(event)
            continue

        if not _quantities_match(expected_quantity, 0.0) or not _quantities_match(
            event.remaining_quantity, 0.0
        ):
            raise _lifecycle_error(event, "close must fully flatten the position")
        state.events.append(event)
        lifecycles.append(
            _PositionLifecycle(
                symbol=event.symbol,
                ordinal=state.ordinal,
                start_sequence=state.start_sequence,
                side=state.side,
                events=tuple(state.events),
                status="complete",
            )
        )
        del active[event.symbol]

    for symbol, state in active.items():
        lifecycles.append(
            _PositionLifecycle(
                symbol=symbol,
                ordinal=state.ordinal,
                start_sequence=state.start_sequence,
                side=state.side,
                events=tuple(state.events),
                status="incomplete",
            )
        )
    return sorted(lifecycles, key=lambda lifecycle: lifecycle.start_sequence)


def _validate_direction(direction: str) -> SignalDirection:
    if direction not in {"long", "short"}:
        raise ValueError("direction must be 'long' or 'short'")
    return direction


def _validate_positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _normalize_ohlcv(
    ohlcv: pd.DataFrame,
    *,
    price_col: str | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Validate single-symbol OHLCV and normalize aware indexes to UTC."""
    if not isinstance(ohlcv.index, pd.DatetimeIndex):
        raise TypeError("ohlcv must use a DatetimeIndex")
    if not ohlcv.index.is_monotonic_increasing:
        raise ValueError("ohlcv index must be sorted in increasing order")
    if not ohlcv.index.is_unique:
        raise ValueError("ohlcv index must contain unique timestamps")

    required = {"high", "low", "close"}
    if price_col is not None:
        if price_col not in {"open", "high", "low", "close"}:
            raise ValueError("price_col must be one of: open, high, low, close")
        required.add(price_col)
    missing = required - set(ohlcv.columns)
    if missing:
        raise ValueError(f"ohlcv is missing required columns: {sorted(missing)}")

    normalized = ohlcv.copy()
    index_is_aware = normalized.index.tz is not None
    if index_is_aware:
        normalized.index = normalized.index.tz_convert("UTC")

    if normalized.empty:
        return normalized, index_is_aware

    values = normalized.loc[:, sorted(required)].to_numpy(dtype=np.float64)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("ohlcv prices must be finite and positive")
    if (normalized["high"] < normalized["low"]).any():
        raise ValueError("ohlcv high must be greater than or equal to low")
    if (normalized["close"] > normalized["high"]).any() or (
        normalized["close"] < normalized["low"]
    ).any():
        raise ValueError("ohlcv close must be within the high/low range")
    if price_col == "open" and (
        (normalized["open"] > normalized["high"]).any()
        or (normalized["open"] < normalized["low"]).any()
    ):
        raise ValueError("ohlcv open must be within the high/low range")
    return normalized, index_is_aware


def _normalize_timestamp(ts: datetime, *, index_is_aware: bool, name: str) -> pd.Timestamp:
    normalized = pd.Timestamp(ts)
    if pd.isna(normalized):
        raise ValueError(f"{name} must not be NaT")
    timestamp_is_aware = normalized.tzinfo is not None
    if timestamp_is_aware != index_is_aware:
        raise ValueError(f"{name} and ohlcv index must both be timezone-aware or both be naive")
    if timestamp_is_aware:
        normalized = normalized.tz_convert("UTC")
    return normalized


def _walk_forward_matrices(
    entries: Sequence[tuple[datetime, float, SignalDirection]],
    ohlcv: pd.DataFrame,
    max_periods: int,
) -> tuple[list[int], np.ndarray, np.ndarray, np.ndarray]:
    """Shared kernel: per-entry-point forward-return/MFE/MAE, walking forward.

    For each ``(ts, price, direction)`` entry, walks up to ``max_periods``
    OHLCV bars strictly after ``ts`` and at each offset T=1..max_periods
    tracks: forward_return (bar T's close vs. entry price), running MFE
    (best favorable excursion up to and including bar T), running MAE
    (worst adverse excursion up to and including bar T) — all
    direction-adjusted so a positive value is favorable.

    This is the walk-forward math shared by trade-entry envelope decay
    (``compute_trade_entry_outcomes``, entries = post-open/add bases,
    ``ts`` is the fill bar itself) and signal-outcome curves
    (``compute_signal_outcomes``, entries = the bar *after* each raw signal,
    since a signal has no fill of its own) — the only difference is what
    supplies the entry points, not the math.

    Excursions are non-negative magnitudes with a zero floor. Entries with
    no bars after them are dropped entirely. Entries with
    fewer than ``max_periods`` bars remaining are NaN-padded past their
    available bars.

    Returns:
        ``(kept_indices, return_mat, mfe_mat, mae_mat)`` — the input index
        of each kept entry and three ``(n_kept, max_periods)`` matrices.
    """
    _validate_positive_integer(max_periods, "max_periods")
    normalized, index_is_aware = _normalize_ohlcv(ohlcv)

    kept_indices: list[int] = []
    return_rows: list[np.ndarray] = []
    mfe_rows: list[np.ndarray] = []
    mae_rows: list[np.ndarray] = []
    for entry_index, (ts, price, direction) in enumerate(entries):
        validated_direction = _validate_direction(direction)
        if not np.isfinite(price) or price <= 0:
            raise ValueError("entry prices must be finite and positive")
        entry_ts = _normalize_timestamp(ts, index_is_aware=index_is_aware, name="entry timestamp")
        # WHY side="right": exact and intra-bar timestamps both resolve to the
        # first actually observed bar strictly after the entry point.
        start_pos = normalized.index.searchsorted(entry_ts, side="right")
        window = normalized.iloc[start_pos : start_pos + max_periods]
        if window.empty:
            continue
        sign = -1.0 if validated_direction == "short" else 1.0
        closes = window["close"].to_numpy(dtype=np.float64)
        highs = window["high"].to_numpy(dtype=np.float64)
        lows = window["low"].to_numpy(dtype=np.float64)
        ret_curve = sign * (closes - price) / price * PERCENTAGE_POINTS_PER_FRACTION
        running_max_h = np.maximum.accumulate(highs)
        running_min_l = np.minimum.accumulate(lows)
        if validated_direction == "short":
            mfe_curve = (price - running_min_l) / price * PERCENTAGE_POINTS_PER_FRACTION
            mae_curve = (running_max_h - price) / price * PERCENTAGE_POINTS_PER_FRACTION
        else:
            mfe_curve = (running_max_h - price) / price * PERCENTAGE_POINTS_PER_FRACTION
            mae_curve = (price - running_min_l) / price * PERCENTAGE_POINTS_PER_FRACTION
        mfe_curve = np.maximum(mfe_curve, 0.0)
        mae_curve = np.maximum(mae_curve, 0.0)
        pad = max_periods - len(ret_curve)
        if pad > 0:
            ret_curve = np.concatenate([ret_curve, np.full(pad, np.nan)])
            mfe_curve = np.concatenate([mfe_curve, np.full(pad, np.nan)])
            mae_curve = np.concatenate([mae_curve, np.full(pad, np.nan)])
        kept_indices.append(entry_index)
        return_rows.append(ret_curve)
        mfe_rows.append(mfe_curve)
        mae_rows.append(mae_curve)

    if not kept_indices:
        empty = np.empty((0, max_periods))
        return kept_indices, empty, empty, empty
    return kept_indices, np.vstack(return_rows), np.vstack(mfe_rows), np.vstack(mae_rows)


def compute_signal_outcomes(
    signal_timestamps: Sequence[datetime],
    ohlcv: pd.DataFrame,
    max_periods: int,
    direction: SignalDirection = "long",
    price_col: SignalPriceColumn = "open",
) -> pd.DataFrame:
    """Per-signal, per-offset forward-return/MFE/MAE curve.

    Computed on demand (nothing persisted), alongside Grafana's own on-demand
    ``_FWD_CTE``/``_EXC_CTE`` panels. Python signal and trade
    callers share ``_walk_forward_matrices``; Grafana keeps its independent
    SQL path, pinned to the same mathematical contract by golden tests.

    Matches the Grafana SQL's reference convention: each signal's reference
    price is the *next* bar's ``price_col`` (default "open"), not the
    signal's own bar. Offset T=1 is the first observed bar after that
    reference bar. Results are gross hypothetical outcomes, not executable
    fills, and are returned in percentage points.

    Args:
        signal_timestamps: Raw signal timestamps (``signal_events.ts``).
        ohlcv: Single-symbol OHLCV DataFrame with DatetimeIndex and
            'open'/'close'/'high'/'low'.
        max_periods: How many bars forward to compute, per signal.
        direction: Expected "long" or "short" price direction for this batch.
            This is independent of an event's entry/exit label.
        price_col: Which next-observed-bar OHLCV field is the reference price;
            matches Grafana's ``$fill_price_field`` variable.

    Returns:
        Long-format DataFrame, one row per (signal, offset) that has data:
        columns ``ts`` (signal timestamp), ``bar_offset``, ``forward_return``,
        ``mfe``, ``mae``. A signal near the end of available OHLCV history
        simply contributes fewer offset rows rather than NaN-padded ones.
    """
    columns = ["ts", "bar_offset", "forward_return", "mfe", "mae"]
    validated_direction = _validate_direction(direction)
    _validate_positive_integer(max_periods, "max_periods")
    normalized, index_is_aware = _normalize_ohlcv(ohlcv, price_col=price_col)
    if not len(signal_timestamps) or normalized.empty:
        return pd.DataFrame(columns=columns)

    entries: list[tuple[datetime, float, SignalDirection]] = []
    resolved_signal_ts: list[datetime] = []
    for sig_ts in signal_timestamps:
        signal_ts = _normalize_timestamp(
            sig_ts, index_is_aware=index_is_aware, name="signal timestamp"
        )
        reference_pos = normalized.index.searchsorted(signal_ts, side="right")
        if reference_pos >= len(normalized):
            continue
        reference_ts = normalized.index[reference_pos]
        reference_price = float(normalized.iloc[reference_pos][price_col])
        entries.append((reference_ts, reference_price, validated_direction))
        resolved_signal_ts.append(sig_ts)

    if not entries:
        return pd.DataFrame(columns=columns)

    kept_indices, return_mat, mfe_mat, mae_mat = _walk_forward_matrices(
        entries, normalized, max_periods
    )

    rows: list[tuple[datetime, int, float, float, float]] = []
    for row_index, entry_index in enumerate(kept_indices):
        sig_ts = resolved_signal_ts[entry_index]
        for offset in range(max_periods):
            ret = return_mat[row_index, offset]
            if np.isnan(ret):
                break  # ran out of forward bars; later offsets are NaN too
            mfe = mfe_mat[row_index, offset]
            mae = mae_mat[row_index, offset]
            rows.append((sig_ts, offset + 1, float(ret), float(mfe), float(mae)))

    return pd.DataFrame(rows, columns=columns)


def summarize_signal_mae_mfe(
    signal_timestamps: Sequence[datetime],
    ohlcv: pd.DataFrame,
    horizons: Sequence[int] = (1, 5, 10, 20, 60),
    direction: SignalDirection = "long",
    price_col: SignalPriceColumn = "open",
) -> pd.DataFrame:
    """Aggregate forward-return/MFE/MAE across signals, at a fixed set of horizons.

    Local-report counterpart to Grafana's on-demand, freely-adjustable-``$n``
    ``signal_monitor`` panels. The Python report uses
    ``compute_signal_outcomes`` -> ``_walk_forward_matrices`` at a small fixed
    set of horizons, while Grafana remains independently implemented in SQL
    against the same golden contract. Nothing is persisted.

    MFE and MAE are non-negative excursion magnitudes in percentage points.
    ``n`` is reported separately at every horizon because recent signals may
    not yet have enough forward observations.

    Returns:
        One row per horizon that has data: horizon, n,
        median_forward_return, median_mfe, p75_mfe, median_mae, p75_mae.
    """
    columns = [
        "horizon",
        "n",
        "median_forward_return",
        "median_mfe",
        "p75_mfe",
        "median_mae",
        "p75_mae",
    ]
    if not horizons:
        return pd.DataFrame(columns=columns)
    validated_horizons = [
        _validate_positive_integer(horizon, "each horizon") for horizon in horizons
    ]
    if len(set(validated_horizons)) != len(validated_horizons):
        raise ValueError("horizons must not contain duplicates")

    outcome_df = compute_signal_outcomes(
        signal_timestamps,
        ohlcv,
        max_periods=max(validated_horizons),
        direction=direction,
        price_col=price_col,
    )
    if outcome_df.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, float | int]] = []
    for h in validated_horizons:
        at_h = outcome_df[outcome_df["bar_offset"] == h]
        if at_h.empty:
            continue
        rows.append(
            {
                "horizon": h,
                "n": len(at_h),
                "median_forward_return": float(at_h["forward_return"].median()),
                "median_mfe": float(at_h["mfe"].median()),
                "p75_mfe": float(at_h["mfe"].quantile(0.75)),
                "median_mae": float(at_h["mae"].median()),
                "p75_mae": float(at_h["mae"].quantile(0.75)),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _normalize_trade_ohlcv(
    symbols: set[str],
    ohlcv_by_symbol: Mapping[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    if isinstance(ohlcv_by_symbol, pd.DataFrame) or not isinstance(ohlcv_by_symbol, Mapping):
        raise TypeError("ohlcv_by_symbol must be a mapping of symbol to OHLCV DataFrame")
    missing = symbols - set(ohlcv_by_symbol)
    if missing:
        raise ValueError(f"missing OHLCV for event symbols: {sorted(missing)}")

    normalized: dict[str, pd.DataFrame] = {}
    for symbol in sorted(symbols):
        frame = ohlcv_by_symbol[symbol]
        if not isinstance(frame, pd.DataFrame):
            raise TypeError(f"OHLCV for {symbol} must be a pandas DataFrame")
        symbol_frame, index_is_aware = _normalize_ohlcv(frame)
        if not index_is_aware:
            raise ValueError(f"OHLCV index for {symbol} must be timezone-aware")
        if symbol_frame.empty:
            raise ValueError(f"OHLCV for {symbol} must not be empty")
        normalized[symbol] = symbol_frame
    return normalized


def _directional_return(mark: float, basis: float, side: SignalDirection) -> float:
    sign = -1.0 if side == "short" else 1.0
    return sign * (mark - basis) / basis * PERCENTAGE_POINTS_PER_FRACTION


def _update_point_excursion(
    mfe: float,
    mae: float,
    *,
    mark: float,
    basis: float,
    side: SignalDirection,
) -> tuple[float, float]:
    outcome = _directional_return(mark, basis, side)
    return max(mfe, outcome, 0.0), max(mae, -outcome, 0.0)


def _update_bar_excursion(
    mfe: float,
    mae: float,
    *,
    bars: pd.DataFrame,
    basis: float,
    side: SignalDirection,
) -> tuple[float, float]:
    if bars.empty:
        return mfe, mae
    max_high = float(bars["high"].max())
    min_low = float(bars["low"].min())
    if side == "short":
        favorable = (basis - min_low) / basis * PERCENTAGE_POINTS_PER_FRACTION
        adverse = (max_high - basis) / basis * PERCENTAGE_POINTS_PER_FRACTION
    else:
        favorable = (max_high - basis) / basis * PERCENTAGE_POINTS_PER_FRACTION
        adverse = (basis - min_low) / basis * PERCENTAGE_POINTS_PER_FRACTION
    return max(mfe, favorable, 0.0), max(mae, adverse, 0.0)


def compute_trade_lifecycle_outcomes(
    order_events: Sequence[OrderEventRecord],
    ohlcv_by_symbol: Mapping[str, pd.DataFrame],
) -> pd.DataFrame:
    """Return one actual-excursion fact row per complete or incomplete position lifecycle.

    Entry basis changes apply prospectively. Full OHLC ranges are used only for
    bars strictly between lifecycle events; event bars contribute their explicit
    fill-price state observations because intrabar ordering is otherwise unknown.
    """
    columns = [
        "symbol",
        "lifecycle_ordinal",
        "status",
        "side",
        "opened_at",
        "closed_at",
        "realized_exits",
        "periods_held",
        "net_pnl",
        "mfe",
        "mae",
    ]
    lifecycles = _reconstruct_position_lifecycles(order_events)
    if not lifecycles:
        return pd.DataFrame(columns=columns)
    normalized = _normalize_trade_ohlcv(
        {lifecycle.symbol for lifecycle in lifecycles}, ohlcv_by_symbol
    )

    rows: list[dict[str, Any]] = []
    for lifecycle in lifecycles:
        frame = normalized[lifecycle.symbol]
        basis = lifecycle.events[0].entry_price
        mfe = 0.0
        mae = 0.0
        previous_ts = _normalize_timestamp(
            lifecycle.events[0].ts, index_is_aware=True, name="event timestamp"
        )

        for event in lifecycle.events[1:]:
            event_ts = _normalize_timestamp(event.ts, index_is_aware=True, name="event timestamp")
            between = frame.loc[(frame.index > previous_ts) & (frame.index < event_ts)]
            mfe, mae = _update_bar_excursion(
                mfe, mae, bars=between, basis=basis, side=lifecycle.side
            )
            mfe, mae = _update_point_excursion(
                mfe, mae, mark=event.price, basis=basis, side=lifecycle.side
            )
            if event.event_type == "add":
                basis = event.entry_price
                mfe, mae = _update_point_excursion(
                    mfe, mae, mark=event.price, basis=basis, side=lifecycle.side
                )
            previous_ts = event_ts

        if lifecycle.status == "incomplete":
            after_last_event = frame.loc[frame.index > previous_ts]
            mfe, mae = _update_bar_excursion(
                mfe,
                mae,
                bars=after_last_event,
                basis=basis,
                side=lifecycle.side,
            )

        exit_events = [
            event for event in lifecycle.events if event.event_type in {"reduce", "close"}
        ]
        close_event = lifecycle.events[-1] if lifecycle.status == "complete" else None
        rows.append(
            {
                "symbol": lifecycle.symbol,
                "lifecycle_ordinal": lifecycle.ordinal,
                "status": lifecycle.status,
                "side": lifecycle.side,
                "opened_at": lifecycle.events[0].ts,
                "closed_at": close_event.ts if close_event is not None else pd.NaT,
                "realized_exits": len(exit_events),
                "periods_held": (close_event.periods_held if close_event is not None else None),
                "net_pnl": float(sum(event.pnl for event in exit_events)),
                "mfe": float(mfe),
                "mae": float(mae),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _summary_groups(
    outcomes: pd.DataFrame,
) -> list[tuple[Literal["pooled", "symbol"], str | None, pd.DataFrame]]:
    groups: list[tuple[Literal["pooled", "symbol"], str | None, pd.DataFrame]] = [
        ("pooled", None, outcomes)
    ]
    groups.extend(
        ("symbol", str(symbol), group) for symbol, group in outcomes.groupby("symbol", sort=True)
    )
    return groups


def summarize_trade_lifecycle_outcomes(outcomes: pd.DataFrame) -> pd.DataFrame:
    """Summarize completed lifecycle MAE/MFE with equal weight per lifecycle."""
    columns = [
        "scope",
        "symbol",
        "n",
        "median_mfe",
        "p75_mfe",
        "median_mae",
        "p75_mae",
    ]
    required = {"symbol", "status", "mfe", "mae"}
    missing = required - set(outcomes.columns)
    if missing:
        raise ValueError(f"lifecycle outcomes are missing columns: {sorted(missing)}")
    completed = outcomes[outcomes["status"] == "complete"]
    if completed.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for scope, symbol, group in _summary_groups(completed):
        rows.append(
            {
                "scope": scope,
                "symbol": symbol,
                "n": len(group),
                "median_mfe": float(group["mfe"].median()),
                "p75_mfe": float(group["mfe"].quantile(0.75)),
                "median_mae": float(group["mae"].median()),
                "p75_mae": float(group["mae"].quantile(0.75)),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def compute_trade_entry_outcomes(
    order_events: Sequence[OrderEventRecord],
    ohlcv_by_symbol: Mapping[str, pd.DataFrame],
    max_periods: int,
) -> pd.DataFrame:
    """Return hypothetical fixed-horizon outcomes for each post-open/add entry basis."""
    columns = [
        "symbol",
        "lifecycle_ordinal",
        "anchor_event_id",
        "anchor_type",
        "anchor_ts",
        "bar_offset",
        "forward_return",
        "mfe",
        "mae",
    ]
    _validate_positive_integer(max_periods, "max_periods")
    lifecycles = _reconstruct_position_lifecycles(order_events)
    if not lifecycles:
        return pd.DataFrame(columns=columns)
    normalized = _normalize_trade_ohlcv(
        {lifecycle.symbol for lifecycle in lifecycles}, ohlcv_by_symbol
    )

    anchors_by_symbol: dict[str, list[tuple[_PositionLifecycle, OrderEventRecord]]] = {}
    for lifecycle in lifecycles:
        anchors_by_symbol.setdefault(lifecycle.symbol, []).extend(
            (lifecycle, event) for event in lifecycle.events if event.event_type in {"open", "add"}
        )

    rows: list[tuple[Any, ...]] = []
    for symbol, anchors in anchors_by_symbol.items():
        entries = [(event.ts, event.entry_price, lifecycle.side) for lifecycle, event in anchors]
        kept_indices, return_mat, mfe_mat, mae_mat = _walk_forward_matrices(
            entries, normalized[symbol], max_periods
        )
        for row_index, anchor_index in enumerate(kept_indices):
            lifecycle, event = anchors[anchor_index]
            for offset_index in range(max_periods):
                forward_return = return_mat[row_index, offset_index]
                if np.isnan(forward_return):
                    break
                rows.append(
                    (
                        symbol,
                        lifecycle.ordinal,
                        event.event_id,
                        event.event_type,
                        event.ts,
                        offset_index + 1,
                        float(forward_return),
                        float(mfe_mat[row_index, offset_index]),
                        float(mae_mat[row_index, offset_index]),
                    )
                )
    return pd.DataFrame(rows, columns=columns)


def summarize_trade_entry_outcomes(outcomes: pd.DataFrame) -> pd.DataFrame:
    """Summarize entry-anchor curves with equal weight and per-horizon valid counts."""
    columns = [
        "scope",
        "symbol",
        "horizon",
        "n",
        "median_forward_return",
        "median_mfe",
        "p75_mfe",
        "median_mae",
        "p75_mae",
    ]
    required = {"symbol", "bar_offset", "forward_return", "mfe", "mae"}
    missing = required - set(outcomes.columns)
    if missing:
        raise ValueError(f"entry outcomes are missing columns: {sorted(missing)}")
    if outcomes.empty:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, Any]] = []
    for scope, symbol, scoped in _summary_groups(outcomes):
        for horizon, group in scoped.groupby("bar_offset", sort=True):
            rows.append(
                {
                    "scope": scope,
                    "symbol": symbol,
                    "horizon": int(horizon),
                    "n": len(group),
                    "median_forward_return": float(group["forward_return"].median()),
                    "median_mfe": float(group["mfe"].median()),
                    "p75_mfe": float(group["mfe"].quantile(0.75)),
                    "median_mae": float(group["mae"].median()),
                    "p75_mae": float(group["mae"].quantile(0.75)),
                }
            )
    return pd.DataFrame(rows, columns=columns)


# WHY these two colors specifically: matches the green=favorable/red=adverse
# threshold-coloring convention already used in the Grafana strategy_dashboard
# (app/grafana/generate_dashboards.py) — local HTML reports and Grafana panels
# read as the same visual system instead of picking an arbitrary new palette.
_FAVORABLE = "#2a9d8f"
_ADVERSE = "#e63946"
_NEUTRAL = "#3d5a80"
_MUTED = "#8a8f98"


def _style_axes(ax: Axes) -> None:
    """Shared minimal chart style: no chartjunk, readable on light or dark cards.

    Colors are mid-grey (not pure black) and the figure is saved transparent
    (see generate_trade_tearsheet) so charts sit directly on the card
    background in both light and dark mode without a baked-in white box.
    """
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(_MUTED)
    ax.grid(True, color=_MUTED, linewidth=0.5, alpha=0.25)
    ax.set_axisbelow(True)
    ax.tick_params(colors=_MUTED, labelsize=9)
    ax.title.set_color("#555555")
    ax.title.set_fontsize(11)
    ax.xaxis.label.set_color(_MUTED)
    ax.yaxis.label.set_color(_MUTED)


def _plot_trade_durations(ax: Axes, durations: list[int]) -> None:
    if durations:
        ax.hist(
            durations,
            bins=min(30, max(1, len(set(durations)))),
            color=_NEUTRAL,
            edgecolor="white",
            linewidth=0.5,
        )
    _style_axes(ax)
    ax.set_title("Position Lifecycle Duration")
    ax.set_xlabel("Periods held (bars)")
    ax.set_ylabel("Completed lifecycle count")


def _plot_pnl_by_lifecycle(ax: Axes, pnl_curve: list[float]) -> None:
    x = list(range(1, len(pnl_curve) + 1))
    if pnl_curve:
        arr = np.array(pnl_curve)
        ax.fill_between(x, arr, 0, where=arr >= 0, color=_FAVORABLE, alpha=0.15, linewidth=0)
        ax.fill_between(x, arr, 0, where=arr < 0, color=_ADVERSE, alpha=0.15, linewidth=0)
        ax.plot(x, arr, color=_NEUTRAL, linewidth=1.6, zorder=3)
    ax.axhline(0, color=_MUTED, linewidth=0.7)
    _style_axes(ax)
    ax.set_title("Net PnL by Position Lifecycle")
    ax.set_xlabel("Completed lifecycle #")
    ax.set_ylabel("Cumulative PnL")


def _plot_trade_entry_envelope(ax: Axes, summary: pd.DataFrame) -> None:
    if not summary.empty:
        offsets = summary["horizon"]
        ax.plot(
            offsets,
            summary["median_mfe"],
            color=_FAVORABLE,
            linewidth=1.8,
            label="median MFE",
        )
        ax.plot(
            offsets,
            summary["p75_mfe"],
            color=_FAVORABLE,
            linewidth=1.1,
            linestyle="--",
            label="p75 MFE",
        )
        ax.plot(
            offsets,
            summary["median_mae"],
            color=_ADVERSE,
            linewidth=1.8,
            label="median MAE",
        )
        ax.plot(
            offsets,
            summary["p75_mae"],
            color=_ADVERSE,
            linewidth=1.1,
            linestyle="--",
            label="p75 MAE",
        )
        ax.legend(frameon=False, fontsize=8.5, labelcolor=_MUTED)
    _style_axes(ax)
    coverage = (
        f"n={int(summary['n'].iloc[0])}->{int(summary['n'].iloc[-1])}"
        if not summary.empty
        else "no data"
    )
    ax.set_title(f"Post-Entry MAE/MFE Envelope ({coverage})")
    ax.set_xlabel("Observed bars after open/add anchor")
    ax.set_ylabel("Percentage-point move from active entry basis")


def _plot_signal_mae_mfe_by_horizon(ax: Axes, summary: pd.DataFrame) -> None:
    if summary.empty:
        _style_axes(ax)
        ax.set_title("Signal MAE/MFE by Horizon (no data)")
        return

    x = np.arange(len(summary))
    width = 0.35
    mfe_err = (summary["p75_mfe"] - summary["median_mfe"]).clip(lower=0)
    mae_err = (summary["p75_mae"] - summary["median_mae"]).clip(lower=0)
    ax.bar(x - width / 2, summary["median_mfe"], width, color=_FAVORABLE, label="median MFE")
    ax.bar(x + width / 2, summary["median_mae"], width, color=_ADVERSE, label="median MAE")
    ax.errorbar(
        x - width / 2,
        summary["median_mfe"],
        yerr=[np.zeros(len(summary)), mfe_err],
        fmt="none",
        ecolor=_FAVORABLE,
        alpha=0.6,
        capsize=3,
    )
    ax.errorbar(
        x + width / 2,
        summary["median_mae"],
        yerr=[np.zeros(len(summary)), mae_err],
        fmt="none",
        ecolor=_ADVERSE,
        alpha=0.6,
        capsize=3,
    )
    ax.set_xticks(x)
    ax.set_xticklabels(
        [f"{h}\nn={n}" for h, n in zip(summary["horizon"], summary["n"], strict=True)]
    )
    ax.legend(frameon=False, fontsize=8.5, labelcolor=_MUTED)
    _style_axes(ax)
    ax.set_title("Signal MAE/MFE by Horizon (error bar = p75)")
    ax.set_xlabel("Observed bars after reference")
    ax.set_ylabel("Percentage-point move from reference price")


def _fmt_stat(value: float | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


_TEARSHEET_CSS = """
:root { --bg:#fafafa; --card:#ffffff; --text:#1a1a1a; --muted:#6b7280; --border:#e5e7eb; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15181c; --card:#1e2227; --text:#e8e8e8; --muted:#9aa0a6; --border:#2c3138; }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text);
  max-width: 880px; margin: 0 auto; padding: 2.5rem 1.5rem 4rem;
}
h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.2rem; }
.subtitle { color: var(--muted); font-size: 0.85rem; margin-bottom: 1.75rem; }
.tiles { display: flex; gap: 0.75rem; margin-bottom: 2rem; flex-wrap: wrap; }
.tile {
  flex: 1; min-width: 130px; background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 0.9rem 1.1rem;
}
.tile-value { font-size: 1.35rem; font-weight: 600; }
.tile-label {
  font-size: 0.7rem; color: var(--muted); margin-top: 0.25rem;
  text-transform: uppercase; letter-spacing: 0.04em;
}
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 0.75rem; margin-bottom: 1.25rem;
}
.card img { width: 100%; display: block; }
"""


def _render_tearsheet_html(
    page_title: str,
    heading: str,
    subtitle: str,
    tiles: list[tuple[str, float | int | str | None]],
    chart_builders: list[Callable[[Axes], None]],
) -> str:
    """Shared HTML assembly: stat tiles + stacked chart cards on ``_TEARSHEET_CSS``.

    Used by every ``generate_*_tearsheet``/``generate_*_report`` function so
    they stay visually consistent and none of them re-implements the
    matplotlib-to-base64-to-HTML plumbing.
    """
    import base64
    from io import BytesIO

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def _fig_to_base64(fig: Any) -> str:
        buf = BytesIO()
        # WHY transparent=True: lets each chart sit on the card background
        # (light or dark) instead of a baked-in white box — see _style_axes.
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=130, transparent=True)
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    cards = []
    for builder in chart_builders:
        fig, ax = plt.subplots(figsize=(7.6, 3.1))
        builder(ax)
        cards.append(
            f'<div class="card"><img src="data:image/png;base64,{_fig_to_base64(fig)}"/></div>'
        )

    tile_html = "".join(
        f'<div class="tile"><div class="tile-value">{_fmt_stat(value)}</div>'
        f'<div class="tile-label">{label}</div></div>'
        for label, value in tiles
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{page_title}</title>
<style>{_TEARSHEET_CSS}</style>
</head>
<body>
<h1>{heading}</h1>
<div class="subtitle">{subtitle}</div>
<div class="tiles">{tile_html}</div>
{"".join(cards)}
</body></html>"""


def generate_trade_tearsheet(
    output: BacktestOutput,
    ohlcv_by_symbol: Mapping[str, pd.DataFrame],
    output_path: str = "trade_tearsheet.html",
    max_periods: int = 48,
) -> str:
    """Generate a lifecycle-consistent HTML trade tearsheet.

    Complements ``generate_tearsheet()`` (equity-based, QuantStats, industry
    standard for calendar-rebalanced strategies) with completed position
    lifecycle duration/PnL and hypothetical post-open/add entry envelopes.
    Realized exits and completed lifecycles are labeled separately.
    """
    order_events = output.order_events
    lifecycle_outcomes = compute_trade_lifecycle_outcomes(order_events, ohlcv_by_symbol)
    completed = lifecycle_outcomes[lifecycle_outcomes["status"] == "complete"].sort_values(
        ["closed_at", "symbol", "lifecycle_ordinal"]
    )
    durations = [int(value) for value in completed["periods_held"].dropna()]
    pnl_curve = [float(value) for value in completed["net_pnl"].cumsum().to_numpy(dtype=np.float64)]
    entry_outcomes = compute_trade_entry_outcomes(
        order_events, ohlcv_by_symbol, max_periods=max_periods
    )
    entry_summary = summarize_trade_entry_outcomes(entry_outcomes)
    pooled_entry_summary = entry_summary[entry_summary["scope"] == "pooled"]
    entry_anchors = (
        int(entry_outcomes["anchor_event_id"].nunique()) if not entry_outcomes.empty else 0
    )
    symbols = ", ".join(sorted({event.symbol for event in order_events})) or "no symbols"
    m = output.metrics

    html = _render_tearsheet_html(
        page_title=f"Trade Tearsheet — {output.run_metadata.run_id}",
        heading="Trade Tearsheet",
        subtitle=(f"{output.run_metadata.strategy} · {symbols} · {output.run_metadata.timeframe}"),
        tiles=[
            ("Realized exits", m.trades),
            ("Completed lifecycles", len(completed)),
            ("Entry anchors", entry_anchors),
            ("Exit win rate", m.win_rate),
            ("Exit profit factor", m.profit_factor),
        ],
        chart_builders=[
            lambda ax: _plot_trade_durations(ax, durations),
            lambda ax: _plot_pnl_by_lifecycle(ax, pnl_curve),
            lambda ax: _plot_trade_entry_envelope(ax, pooled_entry_summary),
        ],
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


def generate_signal_mae_mfe_report(
    signal_timestamps: Sequence[datetime],
    ohlcv: pd.DataFrame,
    output_path: str = "signal_mae_mfe.html",
    horizons: Sequence[int] = (1, 5, 10, 20, 60),
    direction: SignalDirection = "long",
    price_col: SignalPriceColumn = "open",
) -> str:
    """Generate a descriptive local signal-outcome report.

    Outcomes are gross and hypothetical: the next observed bar's
    ``price_col`` is the reference price, offset 1 starts on the following
    observed bar, and no costs or execution constraints are modeled. The
    report shows a separate valid sample count for each horizon and makes no
    statistical-significance claim.
    """
    summary = summarize_signal_mae_mfe(
        signal_timestamps, ohlcv, horizons=horizons, direction=direction, price_col=price_col
    )
    sample_counts = (
        ", ".join(f"T+{h}: {n}" for h, n in zip(summary["horizon"], summary["n"], strict=True))
        if not summary.empty
        else "none"
    )

    html = _render_tearsheet_html(
        page_title="Signal Outcome Report",
        heading="Signal Outcome Report",
        subtitle=(
            f"Gross hypothetical outcomes · direction={direction} · "
            f"reference=next observed {price_col} · offset 1=following observed bar · "
            "no costs or execution constraints"
        ),
        tiles=[
            ("Signals", len(signal_timestamps)),
            ("Direction", direction),
            ("Reference", f"next {price_col}"),
            ("Valid samples", sample_counts),
        ],
        chart_builders=[lambda ax: _plot_signal_mae_mfe_by_horizon(ax, summary)],
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path
