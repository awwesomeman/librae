"""Backtest engine — bar-by-bar execution with Strategy + Executor pattern.

Usage:
    from librae.core.run_config import RunConfig

    bt = Backtest(data=df, strategy=my_strategy, config=config)
    bt.run()
    output = bt.build_output()

The engine owns all position state. Strategies only observe via Context.
Execution uses deterministic simulation functions from core.executor.
Shared PnL calculation uses calc_trade_pnl() from core.executor.

Data format: MultiIndex DataFrame (symbol, datetime) with OHLCV + features.
Single-asset is a special case where symbols has one element.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from numbers import Real
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from librae.backtest.result import (
    AccountBacktestResult,
    AllocationSnapshot,
    BacktestResult,
    EquitySnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
)
from librae.core import EPSILON

if TYPE_CHECKING:
    from librae.backtest.schema import (
        AllocationSnapshotPoint,
        BacktestOutput,
        EquityCurvePoint,
        FinancingCashFlowRecord,
        PositionEventRecord,
        PositionSnapshotPoint,
        StrategyMetrics,
    )
    from librae.core.run_config import RunConfig

from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    REASON_FORCE_CLOSE,
    ExecutionResult,
    ExecutionUnavailableError,
    PortfolioRebalanceState,
    PositionEvent,
    RebalanceOrderState,
    RuntimeEvent,
    TradePnL,
    TradeResult,
    calc_equity,
    calculate_position_weights,
    calculate_signed_position_notionals,
    check_stop_targets,
    coalesce_runtime_events,
    execute_pending_decision_and_stops,
    liquidate_all,
    merge_pending_decisions,
    partition_pending_decision,
    queue_market_exit_all,
    validate_strategy_decision,
)
from librae.core.financing import (
    FinancingCashFlow,
    FinancingLifecycleEvent,
    attribute_financing_to_closes,
    calculate_borrow_cash_flows,
    calculate_funding_cash_flows,
)
from librae.core.liquidity import calculate_lagged_adv
from librae.core.market_data import validate_ohlcv_values
from librae.core.run_config import ExecutionPolicy, MarketDataSessionMode, RiskPolicy
from librae.core.strategy import (
    AccountSnapshot,
    Context,
    OrderIntent,
    PortfolioWeights,
    Position,
    PositionState,
    Strategy,
    StrategyDecision,
)
from librae.core.trading_calendar import (
    ALWAYS_OPEN_CALENDAR,
    period_start,
    session_labels,
    session_ordinals,
    validate_calendar_id,
)
from librae.core.utils import (
    generate_run_id,
    infer_timeframe,
    interval_to_timedelta,
    make_event_id,
    to_canonical,
)

logger = logging.getLogger(__name__)


def _rebalance_symbols(state: PortfolioRebalanceState) -> tuple[str, ...]:
    return tuple(
        sorted(
            {order.intent.symbol for order in state.orders}
            | {leg.symbol for leg in state.unresolved_legs}
        )
    )


def _superseded_rebalance_events(
    ts: datetime,
    state: PortfolioRebalanceState,
    *,
    reason: str = "rebalance_superseded",
) -> list[RuntimeEvent]:
    """Build one persistence-safe cancellation event per symbol.

    ``reason`` names what ended the residual; every path that drops one --
    supersession, a protective exit, a halt -- must leave this audit trail so
    the event log never shows a target that simply stops filling.
    """
    orders_by_symbol: dict[str, list[RebalanceOrderState]] = {}
    for order in state.orders:
        orders_by_symbol.setdefault(order.intent.symbol, []).append(order)

    events: list[RuntimeEvent] = []
    for symbol, raw_orders in orders_by_symbol.items():
        orders = list(raw_orders)
        phases = [
            {
                "phase": order.phase,
                "action": order.intent.action,
                "requested_quantity": order.requested_quantity,
                "filled_quantity": order.requested_quantity - order.remaining_quantity,
                "remaining_quantity": order.remaining_quantity,
            }
            for order in orders
        ]
        detail: dict[str, object] = {
            "reason": reason,
            "quantity_scope": "target",
            "requested_quantity": sum(order.requested_quantity for order in orders),
            "filled_quantity": sum(
                order.requested_quantity - order.remaining_quantity for order in orders
            ),
            "remaining_quantity": sum(order.remaining_quantity for order in orders),
        }
        if len(phases) == 1:
            detail.update({"phase": phases[0]["phase"], "action": phases[0]["action"]})
        else:
            detail["phases"] = phases
        events.append(
            RuntimeEvent(
                ts=ts,
                event_type="decision_skipped",
                symbol=symbol,
                detail=detail,
            )
        )

    for leg in state.unresolved_legs:
        target_notional = abs(leg.target_signed_notional)
        events.append(
            RuntimeEvent(
                ts=ts,
                event_type="decision_skipped",
                symbol=leg.symbol,
                detail={
                    "reason": reason,
                    "sizing_state": "superseded_before_quantity_resolution",
                    "notional_scope": "target_allocation",
                    "requested_notional": target_notional,
                    "filled_notional": 0.0,
                    "remaining_notional": target_notional,
                },
            )
        )
    return events


_INDEX_NAMES = ["symbol", "datetime"]
_MIN_SESSION_CADENCE_SAMPLES = 5


def _validate_backtest_data(
    data: pd.DataFrame,
    configured_symbols: Sequence[str] | None,
) -> None:
    """Validate the point-in-time OHLCV contract at the engine boundary."""
    if not isinstance(data.index, pd.MultiIndex):
        raise ValueError(
            "data must have MultiIndex (symbol, datetime). "
            "For single-asset: df.index = "
            "pd.MultiIndex.from_arrays([['SYM']*len(df), df.index])"
        )
    if data.index.nlevels != 2 or list(data.index.names) != _INDEX_NAMES:
        raise ValueError("data index levels must be exactly ('symbol', 'datetime')")

    if not data.index.is_unique:
        raise ValueError("data index must contain unique (symbol, datetime) pairs")

    symbols = data.index.get_level_values("symbol").unique().tolist()
    if any(not isinstance(symbol, str) or not symbol for symbol in symbols):
        raise ValueError("data symbols must be non-empty strings")
    if configured_symbols is not None:
        if len(configured_symbols) != len(set(configured_symbols)):
            raise ValueError("config.symbols must not contain duplicates")
        actual = set(symbols)
        expected = set(configured_symbols)
        if actual != expected:
            raise ValueError(
                "config.symbols must exactly match data symbols; "
                f"configured={sorted(expected)}, data={sorted(actual)}"
            )

    timestamps = data.index.get_level_values("datetime")
    if not isinstance(timestamps, pd.DatetimeIndex):
        raise ValueError("data datetime index level must contain pandas timestamps")
    if timestamps.tz is None:
        raise ValueError("data datetime index level must be timezone-aware")
    for symbol, symbol_data in data.groupby(level="symbol", sort=False):
        symbol_timestamps = symbol_data.index.get_level_values("datetime")
        if not symbol_timestamps.is_monotonic_increasing:
            raise ValueError(f"data timestamps must be increasing within symbol {symbol!r}")

    validate_ohlcv_values(data)


def _canonicalize_backtest_timestamps(data: pd.DataFrame) -> pd.DataFrame:
    """Return canonical-index data without changing the caller-owned frame."""
    if not isinstance(data.index, pd.MultiIndex) or data.index.nlevels != 2:
        return data
    timestamps = data.index.get_level_values(-1)
    if not isinstance(timestamps, pd.DatetimeIndex) or timestamps.tz is None:
        return data
    utc_timestamps = timestamps.tz_convert("UTC")
    if str(timestamps.tz) == "UTC":
        return data
    normalized = data.copy()
    normalized.index = pd.MultiIndex.from_arrays(
        [data.index.get_level_values(0), utc_timestamps],
        names=data.index.names,
    )
    return normalized


def _terminal_canonical_cadence_start(
    period_ordinals: np.ndarray,
    canonical_starts: np.ndarray,
) -> int | None:
    """Return the start of a terminal run of consecutive canonical periods."""
    minimum = _MIN_SESSION_CADENCE_SAMPLES
    if len(period_ordinals) < minimum * 2 or not canonical_starts[-1]:
        return None

    start = len(period_ordinals) - 1
    while (
        start > 0
        and canonical_starts[start - 1]
        and period_ordinals[start] - period_ordinals[start - 1] == 1
    ):
        start -= 1
    return start if len(period_ordinals) - start >= minimum else None


def _is_exact_daily_prefix(session_ordinal_values: np.ndarray, end: int) -> bool:
    """Return whether the prefix proves one observation per trading session."""
    return end >= _MIN_SESSION_CADENCE_SAMPLES and np.all(
        np.diff(session_ordinal_values[:end]) == 1
    )


def _is_exact_weekly_prefix(
    week_ordinals: np.ndarray,
    week_start_flags: np.ndarray,
    end: int,
) -> bool:
    """Return whether the prefix proves canonical consecutive weekly bars."""
    return (
        end >= _MIN_SESSION_CADENCE_SAMPLES
        and np.all(week_start_flags[:end])
        and np.all(np.diff(week_ordinals[:end]) == 1)
    )


def _infer_symbol_timeframe(index: pd.DatetimeIndex, calendar_id: str | None) -> str:
    """Infer cadence from the complete per-symbol index."""
    if len(index) < _MIN_SESSION_CADENCE_SAMPLES:
        return infer_timeframe(index)
    if calendar_id is None:
        return infer_timeframe(index)

    try:
        ordinals = np.asarray(session_ordinals(index, calendar_id), dtype=np.int64)
        labels = session_labels(index, calendar_id)
    except ValueError:
        return infer_timeframe(index)
    if len(set(ordinals)) != len(ordinals):
        return infer_timeframe(index)

    month_ordinals = pd.PeriodIndex(pd.to_datetime(labels), freq="M").asi8
    month_diffs = np.diff(month_ordinals)
    month_starts = pd.DatetimeIndex(
        [period_start(timestamp, "MN1", calendar_id) for timestamp in index]
    )
    month_start_flags = np.asarray(index == month_starts, dtype=np.bool_)
    week_ordinals = pd.PeriodIndex(pd.to_datetime(labels), freq="W-SUN").asi8
    week_starts = pd.DatetimeIndex(
        [period_start(timestamp, "W1", calendar_id) for timestamp in index]
    )
    week_start_flags = np.asarray(index == week_starts, dtype=np.bool_)

    month_transition_start = _terminal_canonical_cadence_start(
        month_ordinals,
        month_start_flags,
    )
    # Timestamp-only input proves a unit change only when both sides have an
    # exact cadence. Missing observations and non-canonical coarse labels need
    # authoritative subscription metadata rather than another heuristic.
    if month_transition_start is not None and (
        _is_exact_daily_prefix(ordinals, month_transition_start)
        or _is_exact_weekly_prefix(
            week_ordinals,
            week_start_flags,
            month_transition_start,
        )
    ):
        raise ValueError("session cadence changes to MN after earlier denser observations")
    if np.all(month_start_flags) and np.all(month_diffs > 0):
        return f"MN{int(np.gcd.reduce(month_diffs))}"

    week_diffs = np.diff(week_ordinals)
    week_transition_start = _terminal_canonical_cadence_start(
        week_ordinals,
        week_start_flags,
    )
    if week_transition_start is not None and _is_exact_daily_prefix(
        ordinals,
        week_transition_start,
    ):
        raise ValueError("session cadence changes to W after earlier denser observations")
    if np.all(week_start_flags) and np.all(week_diffs > 0):
        return f"W{int(np.gcd.reduce(week_diffs))}"

    session_diffs = np.diff(ordinals)
    return f"D{int(np.gcd.reduce(session_diffs))}"


def _session_timeframe_unit(timeframe: str) -> str | None:
    """Return the calendar cadence unit, keeping day, week, and month distinct."""
    if timeframe.startswith("MN"):
        return "MN"
    if timeframe.startswith(("D", "W")):
        return timeframe[0]
    return None


def _validate_session_timeframe(
    symbol: str,
    index: pd.DatetimeIndex,
    timeframe: str,
    calendar_id: str,
) -> None:
    """Validate calendar cadence and canonical period-start anchors."""
    expected_starts = pd.DatetimeIndex(
        [period_start(timestamp, timeframe, calendar_id) for timestamp in index]
    )
    if np.any(index != expected_starts):
        raise ValueError(
            f"data symbol {symbol!r} timestamps are not canonical "
            f"timeframe={timeframe} period starts"
        )

    if timeframe.startswith("D"):
        interval = int(timeframe[1:])
        ordinals = np.asarray(session_ordinals(index, calendar_id), dtype=np.int64)
    elif timeframe.startswith("W"):
        interval = int(timeframe[1:])
        labels = session_labels(index, calendar_id)
        ordinals = pd.PeriodIndex(pd.to_datetime(labels), freq="W-SUN").asi8
    else:
        interval = int(timeframe[2:])
        labels = session_labels(index, calendar_id)
        ordinals = pd.PeriodIndex(pd.to_datetime(labels), freq="M").asi8
    diffs = np.diff(ordinals)
    if np.any(diffs < interval) or np.any(diffs % interval != 0):
        raise ValueError(
            f"data symbol {symbol!r} timestamps are not aligned to timeframe={timeframe}"
        )


def _resolve_data_timeframe(
    data: pd.DataFrame,
    configured_timeframe: str | None,
    calendar_ids: dict[str, str | None],
) -> str:
    """Infer each symbol independently and require one coherent bar interval."""
    indexes_by_symbol = {
        str(symbol): pd.DatetimeIndex(symbol_data.index.get_level_values("datetime"))
        for symbol, symbol_data in data.groupby(level="symbol", sort=False)
    }
    short_session_samples: dict[str, int] = {}
    for symbol, index in indexes_by_symbol.items():
        calendar_id = calendar_ids.get(symbol)
        if len(index) >= _MIN_SESSION_CADENCE_SAMPLES or calendar_id is None:
            continue
        try:
            ordinals = session_ordinals(index, calendar_id)
        except ValueError:
            continue
        if len(set(ordinals)) == len(ordinals):
            short_session_samples[symbol] = len(index)
    if short_session_samples:
        raise ValueError(
            "cannot validate session cadence: at least five session bars are required "
            f"per symbol; got {short_session_samples}"
        )

    inferred_by_symbol: dict[str, str] = {}
    for symbol, index in indexes_by_symbol.items():
        if len(index) < _MIN_SESSION_CADENCE_SAMPLES:
            continue
        try:
            inferred_by_symbol[symbol] = _infer_symbol_timeframe(
                index,
                calendar_ids.get(symbol),
            )
        except ValueError as exc:
            raise ValueError(f"data symbol {symbol!r} {exc}") from exc
    if configured_timeframe is not None:
        expected = to_canonical(configured_timeframe)
        expected_session_unit = _session_timeframe_unit(expected)
        mismatches = {
            symbol: timeframe
            for symbol, timeframe in inferred_by_symbol.items()
            if timeframe != expected
            and not (
                expected_session_unit is not None
                and _session_timeframe_unit(timeframe) == expected_session_unit
                and calendar_ids.get(symbol) is not None
            )
        }
        if mismatches:
            raise ValueError(
                f"config.timeframe={expected} does not match per-symbol data "
                f"timeframes={mismatches}"
            )
        data_timeframe = expected
    else:
        if not inferred_by_symbol:
            raise ValueError(
                "cannot infer a data timeframe: at least one symbol requires five bars"
            )
        inferred = set(inferred_by_symbol.values())
        if len(inferred) != 1:
            raise ValueError(f"data symbols have inconsistent timeframes: {inferred_by_symbol}")
        data_timeframe = next(iter(inferred))

    if _session_timeframe_unit(data_timeframe) is not None:
        short_session_samples = {
            symbol: len(index)
            for symbol, index in indexes_by_symbol.items()
            if len(index) < _MIN_SESSION_CADENCE_SAMPLES
        }
        if short_session_samples:
            raise ValueError(
                "cannot validate session cadence: at least five session bars are required "
                f"per symbol; got {short_session_samples}"
            )

    if data_timeframe.startswith(("D", "W", "MN")):
        calendar_validated: set[str] = set()
        for symbol, index in indexes_by_symbol.items():
            calendar_id = calendar_ids.get(symbol)
            if data_timeframe.startswith("MN") and calendar_id is None:
                calendar_id = ALWAYS_OPEN_CALENDAR
            if calendar_id is None:
                continue
            _validate_session_timeframe(symbol, index, data_timeframe, calendar_id)
            calendar_validated.add(symbol)
    else:
        calendar_validated = set()

    base_interval = interval_to_timedelta(data_timeframe)
    for symbol, index in indexes_by_symbol.items():
        if len(index) < 2 or symbol in calendar_validated:
            continue
        diffs = pd.Series(index).diff().dropna()
        if any(diff < base_interval or diff % base_interval != pd.Timedelta(0) for diff in diffs):
            raise ValueError(
                f"data symbol {symbol!r} timestamps are not aligned to timeframe={data_timeframe}"
            )
    return data_timeframe


def _attribute_financing_to_trades(
    trades: Sequence[TradeResult],
    trade_notionals: Sequence[float],
    position_events: Sequence[PositionEvent],
    financing_cash_flows: Sequence[FinancingCashFlow],
) -> list[TradePnL]:
    """Fold causally accrued financing into closed-trade metrics."""
    if any(event.entry_at is None for event in position_events):
        raise RuntimeError("position lifecycle event is missing entry_at")
    lifecycle_events = [
        FinancingLifecycleEvent(
            ts=event.ts,
            symbol=event.symbol,
            entry_at=event.entry_at,
            event_type=event.event_type,
            fill_quantity=event.fill_quantity,
            remaining_quantity=event.remaining_quantity,
        )
        for event in position_events
        if event.entry_at is not None
    ]
    close_events = [event for event in lifecycle_events if event.event_type in ("reduce", "close")]
    if len(close_events) != len(trades):
        raise RuntimeError("closed trades do not match position lifecycle events")
    for trade, event in zip(trades, close_events, strict=True):
        if (
            trade.symbol != event.symbol
            or trade.entry_at != event.entry_at
            or trade.exit_at != event.ts
            or abs(trade.quantity - event.fill_quantity) > EPSILON
        ):
            raise RuntimeError("closed trade identity does not match its lifecycle event")

    financing_by_close = attribute_financing_to_closes(
        lifecycle_events,
        financing_cash_flows,
    )

    trade_pnls = []
    for trade, notional, financing_pnl in zip(
        trades,
        trade_notionals,
        financing_by_close,
        strict=True,
    ):
        financing_return = financing_pnl / notional * 100.0 if notional > EPSILON else 0.0
        trade_pnls.append(
            TradePnL(
                gross_pnl=trade.gross_pnl + financing_pnl,
                net_pnl=trade.net_pnl + financing_pnl,
                commission=trade.commission,
                slippage=trade.slippage,
                tax=trade.tax,
                gross_return=trade.gross_return + financing_return,
                net_return=trade.net_return + financing_return,
                exit_commission=0.0,
                exit_slippage=0.0,
                exit_tax=trade.tax,
            )
        )
    return trade_pnls


# ---------------------------------------------------------------------------
# Backtest class
# ---------------------------------------------------------------------------


class Backtest:
    """Bar-by-bar backtest engine.

    Two supported construction styles:
      - Direct args (initial_balance/data_source below): a
        standalone single-run constructor, no RunConfig needed — the
        standard choice for tests, notebooks, and simple scripts.
      - config=RunConfig: derives cost model/initial_balance/data_source
        from config and additionally resolves a per-symbol CostModel for every
        symbol in a multi-asset run — required when running through the
        CLI/DB pipeline (librae/orchestration/cli.py), which builds a RunConfig.
    Both are first-class; neither is deprecated.

    Args:
        data: MultiIndex DataFrame (symbol, datetime) with OHLCV + features.
              For single-asset, wrap with: pd.MultiIndex.from_arrays([["SYM"]*len(df), df.index])
        strategy: Strategy subclass.
        config: RunConfig — see "config=RunConfig" above.
        initial_balance: Starting cash — direct-args style.
        strategy_name: Override strategy name (default: from config or snake_case of class name).
        cost_model: CostModel directly (for tests or custom cost models).
        data_source: Data source identifier — direct-args style.
        session_mode: Market-data session identity for direct-args data. With
            ``config``, ``config.session_mode`` is the only source.
        record_position_snapshots: Record per-symbol end-of-event positions,
            realized weights, and target-versus-achieved allocations. Off by
            default to avoid O(events × configured symbols) memory growth.
        execution: Fill-price and volume assumptions for direct
            construction. With ``config``, ``config.execution`` is the only source.
        risk: Portfolio risk limits for direct construction. With ``config``,
            ``config.risk`` is the only source.
    """

    def __init__(
        self,
        data: pd.DataFrame,
        strategy: Strategy,
        config: RunConfig | None = None,
        initial_balance: float = 100_000.0,
        *,
        account_id: str = "default",
        currency: str = "USD",
        strategy_name: str | None = None,
        cost_model: CostModel | None = None,
        data_source: str = "",
        session_mode: MarketDataSessionMode | None = None,
        record_position_snapshots: bool = False,
        execution: ExecutionPolicy | None = None,
        risk: RiskPolicy | None = None,
    ) -> None:
        data = _canonicalize_backtest_timestamps(data)
        _validate_backtest_data(data, config.symbols if config is not None else None)
        if config is not None and execution is not None:
            raise ValueError(
                "execution cannot override config.execution; use one configuration source"
            )
        if config is not None and risk is not None:
            raise ValueError("risk cannot override config.risk; use one configuration source")
        if config is not None and session_mode is not None:
            raise ValueError(
                "session_mode cannot override config.session_mode; use one configuration source"
            )
        resolved_session_mode = config.session_mode if config is not None else session_mode
        if resolved_session_mode is None:
            resolved_session_mode = "extended"
        if resolved_session_mode not in ("regular", "extended"):
            raise ValueError(
                f"session_mode must be 'regular' or 'extended', got {resolved_session_mode!r}"
            )
        if (
            isinstance(initial_balance, bool)
            or not isinstance(initial_balance, Real)
            or not np.isfinite(initial_balance)
            or initial_balance <= 0
        ):
            raise ValueError("initial_balance must be finite and positive")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id must be a non-empty string")
        if not isinstance(currency, str) or not currency:
            raise ValueError("currency must be a non-empty string")

        self._data = data
        self._strategy = strategy
        self._config = config
        self._session_mode: MarketDataSessionMode = resolved_session_mode
        self._run_id: str | None = None
        self._timeframe: str | None = None
        self._result: BacktestResult | None = None
        self._metrics: StrategyMetrics | None = None
        self._record_position_snapshots = record_position_snapshots

        self._symbols = (
            list(config.symbols)
            if config is not None
            else data.index.get_level_values(0).unique().tolist()
        )
        self._timeline = sorted(data.index.get_level_values("datetime").unique())
        from librae.config.symbols import load_symbol_registry, resolve_symbol

        registry = load_symbol_registry()

        # Resolve from config or explicit args
        if config is not None:
            self._instruments = {symbol: resolve_symbol(config, symbol) for symbol in self._symbols}
            self._account_id = config.account_id
            self._currency = config.account.currency
            self._initial_cash = config.account.initial_cash
            self._data_source = config.data_source
            resolved_name = config.strategy_name
            resolved_cm = CostModel.from_config(config, override=cost_model)
        else:
            self._instruments = {symbol: registry.get(symbol) for symbol in self._symbols}
            self._account_id = account_id
            self._currency = currency
            self._initial_cash = initial_balance
            self._data_source = data_source
            resolved_name = None
            resolved_cm = cost_model if cost_model is not None else CostModel.zero()
        self._calendar_ids = {
            symbol: instrument.calendar_id if instrument is not None else None
            for symbol, instrument in self._instruments.items()
        }

        self._cost_models: dict[str, CostModel] = {"__default__": resolved_cm}
        if config is not None and cost_model is None:
            # Resolve every other symbol in this run independently (each
            # against its own registry entry/cost_overrides/symbol_cost_overrides
            # entry) — a multi-asset run isn't guaranteed to share one
            # multiplier (e.g. tw_futures TXFR1=200 vs MXFR1=50), so only
            # config.symbol (symbols[0]) got a correct CostModel above.
            for sym in self._symbols:
                if sym != config.symbol:
                    self._cost_models[sym] = CostModel.from_config(config, symbol=sym)
        resolved_execution = config.execution if config else execution or ExecutionPolicy()
        self._fill_price = resolved_execution.default_fill_price
        self._max_bar_volume_participation_rate = (
            resolved_execution.max_bar_volume_participation_rate
        )
        self._adv_lookback_sessions = resolved_execution.adv_lookback_sessions
        self._max_adv_participation_rate = resolved_execution.max_adv_participation_rate
        self._max_rebalance_delay_bars = resolved_execution.max_rebalance_delay_bars
        self._rebalance_residual_policy = resolved_execution.rebalance_residual_policy
        self._risk_policy = config.risk if config else risk or RiskPolicy()

        if strategy_name is not None:
            self._strategy_name = strategy_name.lower().replace(" ", "_")
        elif resolved_name:
            self._strategy_name = resolved_name
        else:
            cls_name = type(strategy).__name__
            if cls_name.endswith("Strategy") and len(cls_name) > 8:
                cls_name = cls_name[:-8]
            s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", cls_name)
            self._strategy_name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()

    # --- Public properties ---

    @property
    def result(self) -> BacktestResult:
        """Access BacktestResult. Raises RuntimeError if run() not called."""
        if self._result is None:
            raise RuntimeError("Call run() before accessing result")
        return self._result

    @property
    def run_id(self) -> str:
        """Access run_id. Raises RuntimeError if run() not called."""
        if self._run_id is None:
            raise RuntimeError("Call run() before accessing run_id")
        return self._run_id

    @staticmethod
    def _without_halted_account(
        decision: StrategyDecision,
        halted: bool,
    ) -> StrategyDecision:
        """Discard exposure decisions after the account is drawdown-halted."""
        return [] if halted else decision

    def _execute_steps(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        cash: float,
        decision: StrategyDecision,
        bars: dict[str, dict[str, float]],
        *,
        primary_symbol: str,
        halted: bool,
        get_previous_volume: Callable[[str], float | None],
        get_lagged_adv: Callable[[str], float | None],
        used_adv_quantity_by_symbol: dict[str, float],
        exposure_prices: dict[str, float],
        rebalance_state: PortfolioRebalanceState | None = None,
    ) -> tuple[float, ExecutionResult]:
        if halted:
            result = check_stop_targets(
                positions,
                bars,
                ts,
                get_cost_model=self._get_cost_model,
                max_bar_volume_participation_rate=self._max_bar_volume_participation_rate,
                max_adv_participation_rate=self._max_adv_participation_rate,
                get_volume=get_previous_volume,
                get_lagged_adv=get_lagged_adv,
                used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
                get_executable_quantity=self._get_executable_quantity,
            )
            return cash + result.cash_delta, result
        max_position_notional = None
        if self._risk_policy.max_position_weight:
            execution_equity, _ = calc_equity(
                cash,
                positions,
                get_price=lambda symbol, _position: exposure_prices[symbol],
                get_cost_model=self._get_cost_model,
            )
            max_position_notional = self._risk_policy.max_position_weight * max(
                execution_equity, 0.0
            )
        return execute_pending_decision_and_stops(
            ts,
            positions,
            cash,
            decision,
            bars,
            get_cost_model=self._get_cost_model,
            default_fill=self._fill_price,
            primary_symbol=primary_symbol,
            max_position_notional=max_position_notional,
            max_order_notional=self._risk_policy.max_order_notional,
            max_bar_volume_participation_rate=self._max_bar_volume_participation_rate,
            max_adv_participation_rate=self._max_adv_participation_rate,
            get_previous_volume=get_previous_volume,
            get_lagged_adv=get_lagged_adv,
            used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
            max_gross_exposure=self._risk_policy.max_gross_exposure,
            max_net_exposure=self._risk_policy.max_net_exposure,
            exposure_prices=exposure_prices,
            rebalance_state=rebalance_state,
            rebalance_residual_policy=self._rebalance_residual_policy,
            get_executable_quantity=self._get_executable_quantity,
            validate_intent_prices=self._validate_intent_prices,
            get_min_notional=self._get_min_notional,
        )

    # --- Private helpers ---

    def _get_cost_model(self, symbol: str) -> CostModel:
        """Get a symbol override or the constructor-created default model."""
        return self._cost_models.get(symbol, self._cost_models["__default__"])

    def _get_executable_quantity(self, symbol: str, quantity: float) -> float:
        """Apply configured instrument size rules; unknown direct symbols stay continuous."""
        instrument = self._instruments.get(symbol)
        return instrument.normalize_quantity(quantity) if instrument is not None else quantity

    def _validate_intent_prices(self, symbol: str, intent: OrderIntent) -> None:
        """Apply authoritative instrument price grids before matching."""
        instrument = self._instruments.get(symbol)
        if instrument is not None:
            instrument.validate_order_prices(intent)

    def _get_min_notional(self, symbol: str) -> float | None:
        """Return the shared entry-order minimum, when configured."""
        instrument = self._instruments.get(symbol)
        return instrument.min_notional if instrument is not None else None

    def run(self) -> BacktestResult:
        """Execute the backtest. Generates run_id at start. Returns BacktestResult."""
        self._timeframe = _resolve_data_timeframe(
            self._data,
            self._config.timeframe if self._config is not None else None,
            self._calendar_ids,
        )
        if self._adv_lookback_sessions is not None and self._timeframe != "D1":
            missing_calendars = sorted(
                symbol for symbol, calendar_id in self._calendar_ids.items() if calendar_id is None
            )
            if missing_calendars:
                raise ValueError(
                    "intraday ADV requires calendar_id for every symbol; missing "
                    f"{missing_calendars}"
                )
            for calendar_id in self._calendar_ids.values():
                validate_calendar_id(calendar_id)
        self._run_id = generate_run_id(self._strategy_name, self._symbols[0], self._timeframe)
        logger.info("Backtest started: run_id=%s", self._run_id)

        # WHY: pre-convert all bars once — avoids per-bar DataFrame.to_dict()
        # which is the dominant cost in the hot loop.
        all_bars = self._precompute_bars()
        all_lagged_adv = self._precompute_lagged_adv()
        all_session_labels = self._precompute_session_labels()

        cash = self._initial_cash
        positions: dict[str, PositionState] = {}
        trades: list[TradeResult] = []
        all_events: list[PositionEvent] = []
        equity_curve: list[EquitySnapshot] = []
        exposed_periods = 0
        primary_symbol = self._symbols[0]
        universe = set(self._symbols)
        pending_decision: StrategyDecision = []
        pending_rebalance: PortfolioRebalanceState | None = None
        position_snapshots: list[PositionSnapshot] = []
        allocation_snapshots: list[AllocationSnapshot] = []
        financing_cash_flows: list[FinancingCashFlow] = []
        runtime_events: list[RuntimeEvent] = []
        portfolio_snapshots: list[PortfolioSnapshot] = []
        active_target_weights: dict[str, float] | None = None
        last_prices: dict[str, float] = {}
        decision_index = 0
        equity_peak = self._initial_cash
        halted = False
        adv_session_by_symbol: dict[str, object] = {}
        used_adv_quantity_by_symbol: dict[str, float] = {}
        previous_volumes: dict[str, float] = {}
        rebalance_delay_bars = 0
        unavailable_rebalance_symbols: tuple[str, ...] = ()

        for ts in self._timeline:
            event_start_index = len(all_events)
            bars = all_bars[ts]
            lagged_adv = all_lagged_adv.get(ts, {})
            current_session_labels = all_session_labels.get(ts, {})
            for symbol, label in current_session_labels.items():
                if adv_session_by_symbol.get(symbol) != label:
                    adv_session_by_symbol[symbol] = label
                    used_adv_quantity_by_symbol[symbol] = 0.0

            def get_lagged_adv(
                symbol: str,
                values: dict[str, float] = lagged_adv,
            ) -> float | None:
                return values.get(symbol)

            def get_previous_volume(
                symbol: str,
                values: dict[str, float] = previous_volumes,
            ) -> float | None:
                return values.get(symbol)

            exposure_prices = dict(last_prices)
            for symbol, bar in bars.items():
                open_price = bar.get("open")
                if open_price is not None and np.isfinite(open_price) and open_price > 0:
                    exposure_prices[symbol] = float(open_price)
                close = bar.get("close")
                if close is not None and np.isfinite(close) and close > 0:
                    last_prices[symbol] = float(close)

            pending_decision = self._without_halted_account(pending_decision, halted)
            decision_to_execute, pending_decision = partition_pending_decision(
                pending_decision,
                bars,
                positions,
                primary_symbol=primary_symbol,
            )
            if pending_rebalance is not None and decision_to_execute:
                raise ValueError(
                    "cannot execute a new decision while a rebalance residual is pending"
                )

            # ── Steps 1+1.5: fill the previous pending decision at current
            # bar's price, then check stop-loss/take-profit — shared with
            # LiveTrader's simulation mode so deterministic runtimes cannot
            # drift on this sequence ──
            try:
                cash, step_result = self._execute_steps(
                    ts,
                    positions,
                    cash,
                    decision_to_execute,
                    bars,
                    primary_symbol=primary_symbol,
                    halted=halted,
                    get_previous_volume=get_previous_volume,
                    get_lagged_adv=get_lagged_adv,
                    used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
                    exposure_prices=exposure_prices,
                    rebalance_state=pending_rebalance,
                )
                # WHY: only an executed target is the active one. Recording it
                # before this call would let a deferred target show up in the
                # allocation snapshots of every deferral bar, with drift
                # measured against a book that still reflects the last target.
                if isinstance(decision_to_execute, PortfolioWeights):
                    active_target_weights = dict(decision_to_execute.weights)
            # WHY: catch the category, not one reason. Any condition that only
            # this bar cannot satisfy reaches the same bounded retry, and the
            # raised instance keeps naming which one it was.
            except ExecutionUnavailableError as exc:
                if not isinstance(decision_to_execute, PortfolioWeights):
                    raise
                if self._rebalance_residual_policy == "fail":
                    raise
                if rebalance_delay_bars >= self._max_rebalance_delay_bars:
                    if self._max_rebalance_delay_bars == 0:
                        raise
                    raise ValueError(
                        "PortfolioWeights exceeded "
                        f"max_rebalance_delay_bars={self._max_rebalance_delay_bars} "
                        f"for {list(exc.symbols)} at {ts}: {exc}"
                    ) from exc
                rebalance_delay_bars += 1
                unavailable_rebalance_symbols = exc.symbols
                pending_decision = decision_to_execute
                logger.info(
                    "Deferring PortfolioWeights at %s (%d/%d bars): %s",
                    ts,
                    rebalance_delay_bars,
                    self._max_rebalance_delay_bars,
                    exc,
                )
                cash, step_result = self._execute_steps(
                    ts,
                    positions,
                    cash,
                    [],
                    bars,
                    primary_symbol=primary_symbol,
                    halted=halted,
                    get_previous_volume=get_previous_volume,
                    get_lagged_adv=get_lagged_adv,
                    used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
                    exposure_prices=exposure_prices,
                )
            else:
                was_rebalance = (
                    isinstance(decision_to_execute, PortfolioWeights)
                    or pending_rebalance is not None
                )
                pending_rebalance = step_result.pending_rebalance
                if was_rebalance and pending_rebalance is not None:
                    residual_symbols = _rebalance_symbols(pending_rebalance)
                    if rebalance_delay_bars >= self._max_rebalance_delay_bars:
                        raise ValueError(
                            "PortfolioWeights exceeded "
                            f"max_rebalance_delay_bars={self._max_rebalance_delay_bars}; "
                            f"residual remains for {list(residual_symbols)} at {ts}"
                        )
                    rebalance_delay_bars += 1
                    unavailable_rebalance_symbols = residual_symbols
                elif was_rebalance:
                    rebalance_delay_bars = 0
                    unavailable_rebalance_symbols = ()
            trades.extend(step_result.trades)
            all_events.extend(step_result.events)
            runtime_events.extend(step_result.runtime_events)
            _, event_financing_cash_flows = calculate_funding_cash_flows(
                ts,
                bars,
                positions,
                get_cost_model=self._get_cost_model,
            )
            _, event_borrow_cash_flows = calculate_borrow_cash_flows(
                ts,
                bars,
                positions,
                get_cost_model=self._get_cost_model,
            )
            event_financing_cash_flows.extend(event_borrow_cash_flows)
            for cash_flow in event_financing_cash_flows:
                cash += cash_flow.cash_flow
            financing_cash_flows.extend(event_financing_cash_flows)
            # ── Step 2: equity and drawdown check ──
            mtm, position_view = self._calc_equity_snapshot(
                cash,
                positions,
                last_prices,
            )
            if positions:
                exposed_periods += 1
            equity_peak = max(equity_peak, mtm)
            drawdown = (mtm - equity_peak) / equity_peak if equity_peak > 0 else 0.0
            breached = bool(
                self._risk_policy.max_drawdown_rate
                and not halted
                and drawdown <= -self._risk_policy.max_drawdown_rate
            )
            equity_curve.append(EquitySnapshot(ts=ts, equity=mtm))
            event_turnover = (
                sum(event.notional for event in all_events[event_start_index:]) / mtm
                if mtm > EPSILON
                else 0.0
            )
            portfolio_snapshots.append(
                self._snapshot_portfolio(
                    ts,
                    positions,
                    last_prices,
                    mtm,
                    event_turnover,
                )
            )
            if self._record_position_snapshots:
                position_snapshots.extend(
                    self._snapshot_positions(
                        ts,
                        positions,
                        last_prices,
                        mtm,
                    )
                )
                allocation_snapshots.extend(
                    self._snapshot_allocations(
                        ts,
                        positions,
                        last_prices,
                        mtm,
                        active_target_weights,
                    )
                )
            account_snapshot = AccountSnapshot(
                currency=self._currency,
                cash=cash,
                equity=mtm,
            )

            if breached:
                queue_market_exit_all(
                    positions,
                    reason=REASON_DRAWDOWN_BREACH,
                )
                halted = True
                logger.warning(
                    "Backtest account %s halted at %s: drawdown %.2f%% breached "
                    "max_drawdown_rate=%.2f%% — market exits queued for next "
                    "observed opens",
                    self._account_id,
                    ts,
                    drawdown * 100,
                    self._risk_policy.max_drawdown_rate * 100,
                )

            # ── Step 3: strategy decision (becomes eligible on a later bar) ──
            if halted:
                if pending_rebalance is not None:
                    runtime_events.extend(
                        _superseded_rebalance_events(
                            ts, pending_rebalance, reason="rebalance_cancelled_by_halt"
                        )
                    )
                pending_decision = []
                pending_rebalance = None
            else:
                ctx = Context(
                    ts=ts,
                    symbol=primary_symbol,
                    symbols=self._symbols,
                    bar=bars.get(primary_symbol, {}),
                    bars=bars,
                    positions=position_view,
                    account_id=self._account_id,
                    account=account_snapshot,
                    period_index=decision_index,
                )
                new_decision = self._strategy.on_bar(ctx)
                validate_strategy_decision(
                    new_decision,
                    universe,
                    primary_symbol=primary_symbol,
                    bars=bars,
                    positions=positions,
                )
                new_decision = self._without_halted_account(new_decision, halted)
                if pending_rebalance is not None:
                    if isinstance(new_decision, PortfolioWeights):
                        runtime_events.extend(_superseded_rebalance_events(ts, pending_rebalance))
                        pending_rebalance = None
                        unavailable_rebalance_symbols = ()
                    elif new_decision:
                        raise ValueError(
                            "cannot emit OrderIntents while a PortfolioWeights "
                            "rebalance is deferred"
                        )
                if isinstance(pending_decision, PortfolioWeights) and isinstance(
                    new_decision, PortfolioWeights
                ):
                    runtime_events.append(
                        RuntimeEvent(
                            ts=ts,
                            event_type="decision_skipped",
                            detail={"reason": "rebalance_superseded"},
                        )
                    )
                pending_decision = merge_pending_decisions(
                    pending_decision,
                    new_decision,
                    primary_symbol=primary_symbol,
                )
                decision_index += 1

            self._increment_periods_held(positions, bars)
            previous_volumes.update(
                {
                    symbol: float(bar["volume"])
                    for symbol, bar in bars.items()
                    if bar.get("volume") is not None
                }
            )

        if (
            isinstance(pending_decision, PortfolioWeights) or pending_rebalance is not None
        ) and rebalance_delay_bars:
            raise ValueError(
                "backtest ended before deferred PortfolioWeights could execute; "
                f"still blocked on {list(unavailable_rebalance_symbols)}"
            )
        # WHY: the final intent is discarded because there is no T+1 bar to fill it.
        if pending_decision:
            logger.warning(
                "Discarding unresolved end-of-run strategy decision: %r", pending_decision
            )
        unresolved_risk_exits = sorted(
            symbol
            for symbol, position in positions.items()
            if position.pending_market_exit_reason is not None
        )
        if unresolved_risk_exits:
            raise ValueError(
                "cannot execute queued risk exits without a subsequent tradable bar: "
                f"{unresolved_risk_exits}"
            )
        # Force-close all open positions at last bar
        if self._timeline:
            last_ts = self._timeline[-1]
            last_bars = all_bars[last_ts]
            used_bar_quantity = self._filled_quantities(
                event for event in all_events if event.ts == last_ts
            )
            close_result = liquidate_all(
                positions,
                last_bars,
                last_ts,
                get_cost_model=self._get_cost_model,
                reason=REASON_FORCE_CLOSE,
                max_bar_volume_participation_rate=self._max_bar_volume_participation_rate,
                max_adv_participation_rate=self._max_adv_participation_rate,
                get_lagged_adv=lambda symbol: all_lagged_adv.get(last_ts, {}).get(symbol),
                used_bar_quantity_by_symbol=used_bar_quantity,
                used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
                get_executable_quantity=self._get_executable_quantity,
            )
            trades.extend(close_result.trades)
            all_events.extend(close_result.events)
            cash += close_result.cash_delta
            # WHY: the last bar's liquidity budget can be too small to absorb a
            # position, and there is no later bar to retry on. Report what is
            # left rather than discarding the whole run — the residual is
            # carried into the terminal marks below, so equity stays honest.
            runtime_events.extend(close_result.runtime_events)
            # WHY: forced liquidation happens after the bar snapshot. Replace
            # that point so the curve, metrics, and final account cash reconcile
            # without creating a duplicate timestamp.
            terminal_equity, _ = self._calc_equity_snapshot(cash, positions, last_prices)
            if equity_curve:
                equity_curve[-1] = EquitySnapshot(ts=last_ts, equity=terminal_equity)
            terminal_events = [event for event in all_events if event.ts == last_ts]
            portfolio_snapshots[-1] = self._snapshot_portfolio(
                last_ts,
                positions,
                last_prices,
                terminal_equity,
                (
                    sum(event.notional for event in terminal_events) / terminal_equity
                    if terminal_equity > EPSILON
                    else 0.0
                ),
                exposed=portfolio_snapshots[-1].exposed,
            )
            if self._record_position_snapshots:
                position_snapshots = [
                    snapshot for snapshot in position_snapshots if snapshot.ts != last_ts
                ]
                position_snapshots.extend(
                    self._snapshot_positions(last_ts, positions, last_prices, terminal_equity)
                )
                allocation_snapshots = [
                    snapshot for snapshot in allocation_snapshots if snapshot.ts != last_ts
                ]
                allocation_snapshots.extend(
                    self._snapshot_allocations(
                        last_ts,
                        positions,
                        last_prices,
                        terminal_equity,
                        active_target_weights,
                    )
                )

        self._result = BacktestResult(
            trades=trades,
            position_events=all_events,
            position_snapshots=position_snapshots,
            allocation_snapshots=allocation_snapshots,
            financing_cash_flows=financing_cash_flows,
            runtime_events=coalesce_runtime_events(runtime_events),
            account=AccountBacktestResult(
                account_id=self._account_id,
                currency=self._currency,
                equity_curve=tuple(equity_curve),
                portfolio_snapshots=tuple(portfolio_snapshots),
                initial_cash=self._initial_cash,
                # Terminal liquidation can leave an unfilled residual position.
                # Account equity must include its final valuation mark, as the
                # equity curve and metrics already do.
                final_equity=equity_curve[-1].equity,
                exposed_periods=exposed_periods,
            ),
        )
        return self._result

    def build_output(self) -> BacktestOutput:
        """Compute metrics + build canonical output in one call.

        All metadata is auto-derived from the engine state:
        - run_id: generated in run()
        - started_at/ended_at: from self._timeline
        - symbol: from self._symbols[0]
        - strategy_name: from type(strategy).__name__
        - timeframe: inferred from data index

        Raises RuntimeError if called before run().
        """
        from librae.backtest.schema import (
            AccountPerformance,
            BacktestOutput,
            RunMetadata,
        )
        from librae.core.metrics import compute_all

        if self._result is None:
            raise RuntimeError("Call run() before build_output()")

        result = self._result
        run_id = self._run_id

        timeline = self._timeline
        started_at = (
            timeline[0].to_pydatetime() if hasattr(timeline[0], "to_pydatetime") else timeline[0]
        )
        ended_at = (
            timeline[-1].to_pydatetime() if hasattr(timeline[-1], "to_pydatetime") else timeline[-1]
        )
        timeframe = self._timeframe

        account = result.account
        trade_notionals = [
            abs(trade.entry_price * trade.quantity * self._get_cost_model(trade.symbol).multiplier)
            for trade in result.trades
        ]
        trade_pnls = _attribute_financing_to_trades(
            result.trades,
            trade_notionals,
            result.position_events,
            result.financing_cash_flows,
        )
        metrics = compute_all(
            equity_values=[snapshot.equity for snapshot in account.equity_curve],
            timestamps=[snapshot.ts for snapshot in account.equity_curve],
            trade_pnls=trade_pnls,
            total_periods=len(account.equity_curve),
            exposed_periods=account.exposed_periods,
            trade_notionals=trade_notionals,
            trade_group_ids=[trade.group_id for trade in result.trades],
            trade_entry_ats=[trade.entry_at for trade in result.trades],
            turnover_values=[snapshot.turnover for snapshot in account.portfolio_snapshots],
            gross_exposure_values=[
                snapshot.gross_exposure for snapshot in account.portfolio_snapshots
            ],
            net_exposure_values=[snapshot.net_exposure for snapshot in account.portfolio_snapshots],
            concentration_values=[
                snapshot.concentration for snapshot in account.portfolio_snapshots
            ],
        )
        account_output = AccountPerformance(
            account_id=account.account_id,
            currency=account.currency,
            initial_cash=account.initial_cash,
            final_equity=account.final_equity,
            net_pnl=account.final_equity - account.initial_cash,
            equity_curve=tuple(
                self._enrich_equity_curve(
                    account.equity_curve,
                    account.portfolio_snapshots,
                )
            ),
            metrics=metrics,
        )
        self._metrics = metrics

        run_metadata = RunMetadata(
            run_id=run_id,
            strategy_name=self._strategy_name,
            symbols=tuple(self._symbols),
            timeframe=timeframe,
            data_source=self._data_source,
            started_at=started_at,
            ended_at=ended_at,
            run_at=datetime.now(tz=UTC),
            session_mode=self._session_mode,
        )

        event_records = self._build_event_records(result, run_id)
        position_snapshot_points = self._build_position_snapshot_records(result)
        allocation_snapshot_points = self._build_allocation_snapshot_records(result)
        financing_cash_flow_records = self._build_financing_cash_flow_records(result)

        return BacktestOutput(
            run_metadata=run_metadata,
            account=account_output,
            position_events=tuple(event_records),
            position_snapshots=tuple(position_snapshot_points),
            allocation_snapshots=tuple(allocation_snapshot_points),
            financing_cash_flows=tuple(financing_cash_flow_records),
            runtime_events=tuple(result.runtime_events),
        )

    @property
    def metrics(self) -> StrategyMetrics:
        if self._metrics is None:
            raise RuntimeError("Call build_output() before accessing metrics")
        return self._metrics

    def _build_event_records(
        self,
        result: BacktestResult,
        run_id: str,
    ) -> list[PositionEventRecord]:
        """Map PositionEvent -> PositionEventRecord."""
        from librae.backtest.schema import PositionEventRecord

        return [
            PositionEventRecord(
                event_id=make_event_id(run_id, i),
                ts=e.ts,
                account_id=self._account_id,
                currency=self._currency,
                symbol=e.symbol,
                side=e.side,
                event_type=e.event_type,
                fill_quantity=float(e.fill_quantity),
                price=float(e.price),
                entry_price=float(e.entry_price),
                remaining_quantity=float(e.remaining_quantity),
                notional=float(e.notional),
                commission=float(e.commission),
                slippage=float(e.slippage),
                tax=float(e.tax),
                entry_commission=(
                    float(e.entry_commission) if e.entry_commission is not None else None
                ),
                entry_slippage=(float(e.entry_slippage) if e.entry_slippage is not None else None),
                entry_tax=float(e.entry_tax) if e.entry_tax is not None else None,
                realized_pnl=float(e.realized_pnl) if e.realized_pnl is not None else None,
                net_return=float(e.net_return) if e.net_return is not None else None,
                entry_at=e.entry_at,
                periods_held=e.periods_held,
                reason=e.reason,
                group_id=e.group_id,
                time_in_force=e.time_in_force,
                margin_locked=(float(e.margin_locked) if e.margin_locked is not None else None),
                leverage=float(e.leverage) if e.leverage is not None else None,
                liquidation_price=(
                    float(e.liquidation_price) if e.liquidation_price is not None else None
                ),
                margin_roi=float(e.margin_roi) if e.margin_roi is not None else None,
                margin_mode=e.margin_mode,
                cash_flow=float(e.cash_flow) if e.cash_flow is not None else None,
            )
            for i, e in enumerate(result.position_events)
        ]

    def _build_position_snapshot_records(
        self,
        result: BacktestResult,
    ) -> list[PositionSnapshotPoint]:
        """Map raw engine position snapshots to the canonical output schema."""
        from librae.backtest.schema import PositionSnapshotPoint

        return [
            PositionSnapshotPoint(
                ts=snapshot.ts,
                account_id=self._account_id,
                currency=self._currency,
                symbol=snapshot.symbol,
                side=snapshot.side,
                quantity=float(snapshot.quantity),
                price=float(snapshot.price),
                market_value=float(snapshot.market_value),
                realized_weight=float(snapshot.realized_weight),
            )
            for snapshot in result.position_snapshots
        ]

    def _build_allocation_snapshot_records(
        self,
        result: BacktestResult,
    ) -> list[AllocationSnapshotPoint]:
        """Map target-versus-achieved allocation facts to output schema."""
        from librae.backtest.schema import AllocationSnapshotPoint

        return [
            AllocationSnapshotPoint(
                ts=snapshot.ts,
                account_id=self._account_id,
                currency=self._currency,
                symbol=snapshot.symbol,
                target_weight=snapshot.target_weight,
                realized_weight=float(snapshot.realized_weight),
                weight_drift=snapshot.weight_drift,
            )
            for snapshot in result.allocation_snapshots
        ]

    def _build_financing_cash_flow_records(
        self,
        result: BacktestResult,
    ) -> list[FinancingCashFlowRecord]:
        """Map financing cash flows to the canonical output schema."""
        from librae.backtest.schema import FinancingCashFlowRecord

        return [
            FinancingCashFlowRecord(
                ts=cash_flow.ts,
                account_id=self._account_id,
                currency=self._currency,
                symbol=cash_flow.symbol,
                kind=cash_flow.kind,
                side=cash_flow.side,
                quantity=cash_flow.quantity,
                mark_price=cash_flow.mark_price,
                multiplier=cash_flow.multiplier,
                rate=cash_flow.rate,
                cash_flow=cash_flow.cash_flow,
                group_id=cash_flow.group_id,
                entry_at=cash_flow.entry_at,
            )
            for cash_flow in result.financing_cash_flows
        ]

    @staticmethod
    def _enrich_equity_curve(
        equity_curve: Sequence[EquitySnapshot],
        portfolio_snapshots: Sequence[PortfolioSnapshot],
    ) -> list[EquityCurvePoint]:
        """Build EquityCurvePoints with drawdown and period return."""
        from librae.backtest.schema import EquityCurvePoint

        equity_points: list[EquityCurvePoint] = []
        peak = 0.0
        prev_eq = equity_curve[0].equity if equity_curve else 1.0
        for i, snap in enumerate(equity_curve):
            portfolio = portfolio_snapshots[i]
            eq = snap.equity
            peak = max(peak, eq)
            drawdown = (eq - peak) / peak if peak > 0 else 0.0
            period_return = (eq / prev_eq - 1.0) if prev_eq > 0 else 0.0
            prev_eq = eq

            equity_points.append(
                EquityCurvePoint(
                    ts=snap.ts,
                    equity=float(eq),
                    period_return=float(period_return),
                    drawdown=float(drawdown),
                    gross_exposure=float(portfolio.gross_exposure),
                    net_exposure=float(portfolio.net_exposure),
                    concentration=float(portfolio.concentration),
                    turnover=float(portfolio.turnover),
                    exposed=portfolio.exposed,
                )
            )
        return equity_points

    def _precompute_bars(self) -> dict[pd.Timestamp, dict[str, dict[str, float]]]:
        """Pre-convert all cross-sections to dicts once.

        Eliminates per-bar DataFrame.to_dict() calls in the hot loop.
        Trades O(N_bars) memory for O(1) per-bar lookup.

        A single ``to_dict(orient="index")`` over the whole frame, not
        ``groupby(level="datetime")`` + per-group ``to_dict`` — pandas
        groupby iteration has real per-group construction overhead that
        dominates when there are many small groups (one row per group for
        a single-symbol backtest, which is the common case). Verified
        ~1770x faster on a 97,633-row single-symbol M5 frame (groupby
        ~490 rows/sec vs this ~869k rows/sec), same output.
        """
        result: dict[pd.Timestamp, dict[str, dict[str, float]]] = {}
        raw = self._data.to_dict(orient="index")
        for (sym, ts), row in raw.items():
            result.setdefault(ts, {})[sym] = row
        return result

    def _precompute_lagged_adv(self) -> dict[pd.Timestamp, dict[str, float]]:
        """Precompute point-in-time ADV from completed trading sessions."""
        lookback = self._adv_lookback_sessions
        if lookback is None:
            return {}

        result: dict[pd.Timestamp, dict[str, float]] = {}
        for symbol, symbol_data in self._data.groupby(level="symbol", sort=False):
            volume = symbol_data["volume"].droplevel("symbol")
            labels = None
            if self._timeframe != "D1":
                calendar_id = self._calendar_ids[symbol]
                if calendar_id is None:  # guarded at run() boundary
                    raise RuntimeError(f"missing calendar_id for {symbol}")
                labels = session_labels(pd.DatetimeIndex(volume.index), calendar_id)
            lagged = calculate_lagged_adv(
                volume,
                lookback,
                session_labels=labels,
            )
            for ts, value in lagged.dropna().items():
                result.setdefault(ts, {})[symbol] = float(value)
        return result

    def _precompute_session_labels(self) -> dict[pd.Timestamp, dict[str, object]]:
        """Map every bar to its instrument session for cumulative ADV usage."""
        if self._adv_lookback_sessions is None:
            return {}

        result: dict[pd.Timestamp, dict[str, object]] = {}
        for symbol, symbol_data in self._data.groupby(level="symbol", sort=False):
            timestamps = pd.DatetimeIndex(symbol_data.index.get_level_values("datetime"))
            if self._timeframe == "D1":
                labels: Iterable[object] = timestamps
            else:
                calendar_id = self._calendar_ids[symbol]
                if calendar_id is None:  # guarded at run() boundary
                    raise RuntimeError(f"missing calendar_id for {symbol}")
                labels = session_labels(timestamps, calendar_id)
            for timestamp, label in zip(timestamps, labels, strict=True):
                result.setdefault(timestamp, {})[symbol] = label
        return result

    @staticmethod
    def _increment_periods_held(
        positions: dict[str, PositionState],
        bars: dict[str, dict[str, float]],
    ) -> None:
        """Advance holding age only when that position has a market bar."""
        for symbol, position in positions.items():
            if symbol in bars:
                position.periods_held += 1

    def _calc_equity_snapshot(
        self,
        cash: float,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
    ) -> tuple[float, dict[str, Position]]:
        """Compute portfolio MTM from the latest point-in-time marks."""

        def _price(sym: str, _position: PositionState) -> float:
            try:
                return last_prices[sym]
            except KeyError as exc:
                raise RuntimeError(
                    f"no point-in-time mark available for open position {sym}"
                ) from exc

        return calc_equity(
            cash,
            positions,
            get_price=_price,
            get_cost_model=self._get_cost_model,
        )

    def _snapshot_positions(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
    ) -> list[PositionSnapshot]:
        """Build deterministic end-of-bar position and realized-weight facts."""
        snapshots: list[PositionSnapshot] = []
        signed_notionals = calculate_signed_position_notionals(
            positions,
            prices=last_prices,
            get_cost_model=self._get_cost_model,
        )
        realized_weights = calculate_position_weights(
            positions,
            equity,
            prices=last_prices,
            get_cost_model=self._get_cost_model,
        )
        for symbol in sorted(positions):
            position = positions[symbol]
            price = last_prices[symbol]
            snapshots.append(
                PositionSnapshot(
                    ts=ts,
                    symbol=symbol,
                    side=position.side,
                    quantity=position.quantity,
                    price=price,
                    market_value=signed_notionals[symbol],
                    realized_weight=realized_weights[symbol],
                )
            )
        return snapshots

    def _realized_weights(
        self,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
    ) -> dict[str, float]:
        return calculate_position_weights(
            positions,
            equity,
            prices=last_prices,
            get_cost_model=self._get_cost_model,
        )

    def _snapshot_allocations(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
        target_weights: dict[str, float] | None,
    ) -> list[AllocationSnapshot]:
        """Record every configured symbol, including unfilled targets."""
        realized_weights = self._realized_weights(positions, last_prices, equity)
        snapshots = []
        for symbol in sorted(self._symbols):
            target_weight = target_weights.get(symbol, 0.0) if target_weights is not None else None
            realized_weight = realized_weights.get(symbol, 0.0)
            snapshots.append(
                AllocationSnapshot(
                    ts=ts,
                    symbol=symbol,
                    target_weight=target_weight,
                    realized_weight=realized_weight,
                    weight_drift=(
                        realized_weight - target_weight if target_weight is not None else None
                    ),
                )
            )
        return snapshots

    def _snapshot_portfolio(
        self,
        ts: datetime,
        positions: dict[str, PositionState],
        last_prices: dict[str, float],
        equity: float,
        turnover: float,
        *,
        exposed: bool | None = None,
    ) -> PortfolioSnapshot:
        """Compute end-of-event exposure ratios from signed market values."""
        realized_weights = self._realized_weights(positions, last_prices, equity)
        return PortfolioSnapshot(
            ts=ts,
            gross_exposure=sum(abs(weight) for weight in realized_weights.values()),
            net_exposure=sum(realized_weights.values()),
            concentration=max((abs(weight) for weight in realized_weights.values()), default=0.0),
            turnover=turnover,
            exposed=bool(positions) if exposed is None else exposed,
        )

    @staticmethod
    def _filled_quantities(events: Iterable[PositionEvent]) -> dict[str, float]:
        """Aggregate quantity already matched per symbol in one data event."""
        quantities: dict[str, float] = {}
        for event in events:
            quantities[event.symbol] = quantities.get(event.symbol, 0.0) + event.fill_quantity
        return quantities
