"""Polling engine for shadow simulation and broker-confirmed live execution.

Processes newly completed bars as data-driven events and routes intents to
LiveExecutor. Infrastructure integrations are constructor-injected; deployment
factories belong outside the engine.
"""

from __future__ import annotations

import logging
import signal
import types
from collections import deque
from collections.abc import Callable, Container, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from inspect import getattr_static
from math import isclose, isfinite
from queue import Empty, SimpleQueue
from threading import Lock, RLock
from time import perf_counter
from typing import TYPE_CHECKING, Literal

import pandas as pd

from librae.core import EPSILON
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    REASON_OPERATOR_FLATTEN,
    ExecutionLiquidityUnavailableError,
    ExecutionPriceUnavailableError,
    ExecutionResult,
    ExecutionSideUnavailableError,
    ExecutionUnavailableError,
    PositionEvent,
    RuntimeEvent,
    RuntimeEventType,
    apply_execution_fill,
    calc_equity,
    calculate_position_weights,
    check_stop_targets,
    close_position,
    execute_order_intents,
    execute_pending_decision_and_stops,
    execute_portfolio_weights,
    merge_pending_decisions,
    normalize_order_intents,
    order_side_is_tradable,
    partition_pending_decision,
    queue_market_exit_all,
    refused_short_symbols,
    validate_exposure_transition,
    validate_strategy_decision,
)
from librae.core.financing import (
    attach_borrow_rate,
    calculate_borrow_cash_flows,
    calculate_funding_cash_flows,
)
from librae.core.liquidity import calculate_lagged_adv
from librae.core.market_data import (
    AVAILABLE_AT_COLUMN,
    OBSERVED_AT_COLUMN,
    OHLCV_COLUMNS,
    BatchFeatureFn,
    FeatureBatch,
    MarketDataSubscription,
    MarketDataView,
    evaluate_batch_features,
    normalize_bar_times,
    subscription_from_instrument,
    validate_feature_frame,
    validate_ohlcv_values,
)
from librae.core.readiness import next_expected_close
from librae.core.strategy import (
    AccountSnapshot,
    Context,
    Fill,
    OrderIntent,
    PortfolioWeights,
    Position,
    PositionState,
    Strategy,
    StrategyDecision,
)
from librae.core.trading_calendar import (
    execution_session_label,
    session_label,
    session_labels,
    validate_calendar_id,
)

from . import (
    auxiliary_cache,
    data_readiness,
    halt_recovery,
    ohlcv_audit,
    resting_orders,
)
from .execution_identity import (
    ExecutionIdentity,
    account_lease_key,
    resolve_execution_identity,
    runtime_state_key,
)
from .executor import (
    BrokerUnavailableError,
    ExecutionReport,
    LiveExecutor,
    OrderBelowVenueMinimumError,
    OrderRequest,
)
from .interfaces import (
    BarCallback,
    BarDataFetcher,
    FinancingCashFlowCallback,
    HeartbeatCallback,
    Notifier,
    OhlcvBatchCallback,
    OhlcvCallback,
    PerformanceCallback,
    PositionEventCallback,
    RuntimeEventCallback,
    SignalOutcomeCallback,
    WarmupFetcher,
)
from .persistent_alerts import PersistentAlerts
from .state import (
    MAX_PENDING_OHLCV,
    HaltResetReadiness,
    LiveRebalance,
    LiveRuntimeState,
    LiveStateStore,
    PendingOhlcvDelivery,
    TrackedOrder,
    normalize_runtime_revision,
)

if TYPE_CHECKING:
    from librae.config.symbols import SymbolInfo
    from librae.core.cost_model import CostModel
    from librae.core.run_config import MarketDataSessionMode, RunConfig

logger = logging.getLogger(__name__)

# Max distance a funding settlement may sit from a bar timestamp and still
# be attributed to it — far above exchange millisecond jitter, far below any
# supported bar interval.
_FUNDING_TS_TOLERANCE = pd.Timedelta("1min")


@dataclass(frozen=True)
class _BrokerPosition:
    side: Literal["long", "short"]
    quantity: float
    average_price: float | None


@dataclass(frozen=True)
class CycleDiagnostics:
    """Measured runtime latency for the latest completed poll cycle."""

    started_at: datetime
    fetch_seconds_by_symbol: tuple[tuple[str, float], ...]
    strategy_seconds: float
    order_seconds: float
    cycle_seconds: float
    deadline_missed: bool


type _OhlcvAuditRow = tuple[datetime, dict[str, object]]


@dataclass(frozen=True)
class _MarketDataFetchResult:
    """One required symbol's explicit outcome for the current poll."""

    outcome: Literal["success", "error", "terminal"]
    frame: pd.DataFrame | None
    elapsed_seconds: float
    audit_rows: tuple[_OhlcvAuditRow, ...] = ()
    corrections: tuple[_OhlcvAuditRow, ...] = ()
    error: Exception | None = None


def _validate_feature_output(
    output: object,
    *,
    symbol: str,
    event_ts: datetime,
) -> pd.DataFrame:
    """Require one causal, current-event feature frame."""
    return validate_feature_frame(output, label=symbol, event_ts=event_ts)


def _bind_market_data_source(
    source: object,
    instrument: SymbolInfo,
    session_mode: MarketDataSessionMode = "extended",
    *,
    route_owner: str | None = None,
    calendar_id: str | None = None,
) -> BarDataFetcher:
    """Bind a callable fetcher or a concrete adapter to one resolved symbol."""
    resolved_calendar_id = calendar_id if calendar_id is not None else instrument.calendar_id
    fetch_ohlcv = getattr(source, "fetch_ohlcv", None)
    if not callable(fetch_ohlcv):
        if callable(source):
            return source
        raise TypeError(
            "adapter must be a bar-data callable or expose a callable fetch_ohlcv method"
        )

    if route_owner == "ibkr":
        return lambda _symbol, tf, limit, *, drop_incomplete=False: fetch_ohlcv(
            instrument.venue_symbol,
            tf,
            limit=limit,
            security_type=instrument.security_type,
            exchange=instrument.exchange,
            currency=instrument.currency,
            continuous_alias=instrument.continuous_alias,
            contract_month=instrument.contract_month,
            calendar_id=resolved_calendar_id,
            session_mode=session_mode,
            drop_incomplete=drop_incomplete,
        )
    if session_mode != "extended":
        raise ValueError(
            f"data adapter route {route_owner or 'caller-owned'!r} cannot honor "
            f"session_mode={session_mode!r}; provide a session-filtered callable "
            "or use session_mode='extended'"
        )
    if route_owner == "shioaji":
        return lambda _symbol, tf, limit, *, drop_incomplete=False: fetch_ohlcv(
            instrument.venue_symbol,
            tf,
            limit=limit,
            calendar_id=resolved_calendar_id,
            continuous_alias=instrument.continuous_alias,
            contract_month=instrument.contract_month,
            drop_incomplete=drop_incomplete,
        )

    def base_fetcher(_symbol, tf, limit, *, drop_incomplete=False):
        return fetch_ohlcv(
            instrument.venue_symbol,
            tf,
            limit=limit,
            continuous_alias=instrument.continuous_alias,
            contract_month=instrument.contract_month,
            drop_incomplete=drop_incomplete,
        )

    is_perpetual = instrument.instrument_type == "contract_perpetual"
    fetch_funding_rate_history = getattr(source, "fetch_funding_rate_history", None)
    fetch_borrow_rate_history = getattr(source, "fetch_borrow_rate_history", None)
    reported_rate_failures: set[str] = set()

    def _rates_or_none(fetch, kind, *args, **kwargs):
        """Fetch a financing-rate series without letting it take the bars down.

        The rate enriches a bar; it is not the bar. Letting it raise reaches
        _fetch_with_cache's catch-all, which drops the whole symbol back to its
        previous cache -- so a rate endpoint being unreachable would quietly
        freeze market data instead of merely leaving a position uncharged.
        Warn once per binding: a missing rate repeats every cycle, and the
        operator needs to see it, not scroll past it.
        """
        try:
            return fetch(*args, **kwargs)
        except Exception:
            if kind not in reported_rate_failures:
                reported_rate_failures.add(kind)
                logger.warning(
                    "%s %s rates unavailable for %s; bars continue but positions in it "
                    "will not be charged %s until this is resolved",
                    instrument.symbol,
                    kind,
                    instrument.venue_symbol,
                    kind,
                    exc_info=True,
                )
            return None

    # A perpetual's holding cost is funding; anything else that can be sold
    # short is borrowed and pays interest. Never both, or the position is
    # charged twice (see librae.core.financing).
    if is_perpetual and callable(fetch_funding_rate_history):

        def _with_funding(_symbol, tf, limit, *, drop_incomplete=False):
            bars = base_fetcher(_symbol, tf, limit, drop_incomplete=drop_incomplete)
            if bars.empty:
                return bars
            # By time, never by bar count: a warmup spans far more bars than
            # the endpoint accepts as a limit. Start one merge tolerance early
            # so a settlement stamped just before the first bar still lands.
            first_bar = pd.Timestamp(bars["ts"].iloc[0]) - _FUNDING_TS_TOLERANCE
            funding = _rates_or_none(
                fetch_funding_rate_history,
                "funding",
                instrument.venue_symbol,
                since=int(first_bar.timestamp() * 1000),
                limit=None,
            )
            if funding is None or funding.empty:
                return bars
            # A settlement is a discrete payment, so it attaches to the one bar
            # it lands on. Timestamps jitter off the bar grid by milliseconds
            # (Binance fundingTime 08:00:00.003), so an exact-equality merge
            # silently drops roughly half of all payments.
            return pd.merge_asof(
                bars,
                funding.sort_values("ts"),
                on="ts",
                direction="nearest",
                tolerance=_FUNDING_TS_TOLERANCE,
            )

        return _with_funding

    if not is_perpetual and callable(fetch_borrow_rate_history):

        def _with_borrow(_symbol, tf, limit, *, drop_incomplete=False):
            bars = base_fetcher(_symbol, tf, limit, drop_incomplete=drop_incomplete)
            if bars.empty:
                return bars
            # Deliberately not the bar window funding covers. Only the newest
            # bar ever accrues (the engine charges the current event, see
            # _apply_financing_cash_flows), so the fetch needs to reach back
            # one staleness bound, not across the warmup window -- and this
            # endpoint rejects a bar-sized limit outright.
            borrow = _rates_or_none(fetch_borrow_rate_history, "borrow", instrument.venue_symbol)
            if borrow is None or borrow.empty:
                return bars
            # Unlike a funding settlement, a borrow rate is a step function:
            # the last published rate stays in force until the next one, so
            # every bar after it accrues at that rate. A "nearest" join would
            # charge one bar per publication and leave the rest free.
            # Scaling, join direction and staleness all live in one place so a
            # caller's backtest binding expires a rate at the same moment this
            # does -- see librae.core.financing.attach_borrow_rate.
            return attach_borrow_rate(bars, borrow, timeframe=tf)

        return _with_borrow

    return base_fetcher


_MISSING_MARKET_DATA_CAPABILITY = object()


def _validated_reason(reason: object, label: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{label} reason must be a non-empty string")
    return reason.strip()


def _market_data_capability(source: object, name: str) -> str | None:
    """Read one explicitly declared source capability without trusting ``__getattr__``."""
    declaration = getattr_static(source, name, _MISSING_MARKET_DATA_CAPABILITY)
    if declaration is _MISSING_MARKET_DATA_CAPABILITY:
        return None
    value = getattr(source, name)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a non-empty string when supplied")
    if not value.strip():
        raise ValueError(f"{name} must be a non-empty string when supplied")
    if value != value.strip():
        raise ValueError(f"{name} must not contain leading or trailing whitespace")
    return value


def _market_data_route_owner(source: object) -> str | None:
    """Read an adapter's explicit native-route capability without duck-mocking it."""
    return _market_data_capability(source, "market_data_route")


def _market_data_calendar_id(source: object) -> str | None:
    """Read a caller-owned source's explicit subscription calendar capability."""
    return _market_data_capability(source, "market_data_calendar_id")


@dataclass(frozen=True, slots=True)
class _MarketDataSubscriptionSnapshot:
    """One-time resolution of source ownership and exact bar identity."""

    route_owners: Mapping[str, str | None]
    subscriptions: Mapping[str, MarketDataSubscription]

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_owners", types.MappingProxyType(dict(self.route_owners)))
        object.__setattr__(self, "subscriptions", types.MappingProxyType(dict(self.subscriptions)))


@dataclass(frozen=True, slots=True)
class _MarketDataSourceCapabilities:
    """Explicit capabilities read once from one concrete source instance."""

    route_owner: str | None
    calendar_id: str | None


def _read_market_data_source_capabilities(
    sources: Mapping[str, object],
) -> dict[str, _MarketDataSourceCapabilities]:
    """Snapshot each concrete source once, even when an instance serves many symbols."""
    by_source_id: dict[int, _MarketDataSourceCapabilities] = {}
    capabilities: dict[str, _MarketDataSourceCapabilities] = {}
    for symbol, source in sources.items():
        source_id = id(source)
        resolved = by_source_id.get(source_id)
        if resolved is None:
            resolved = _MarketDataSourceCapabilities(
                route_owner=_market_data_route_owner(source),
                calendar_id=_market_data_calendar_id(source),
            )
            by_source_id[source_id] = resolved
        capabilities[symbol] = resolved
    return capabilities


def _resolve_effective_calendars_from_capabilities(
    instruments: Mapping[str, SymbolInfo],
    source_capabilities: Mapping[str, _MarketDataSourceCapabilities],
) -> dict[str, str | None]:
    """Resolve source/config calendars from an already-read capability snapshot."""
    calendars: dict[str, str | None] = {}
    for symbol, instrument in instruments.items():
        configured_calendar = instrument.calendar_id
        capability = source_capabilities.get(symbol)
        source_calendar = capability.calendar_id if capability is not None else None
        if (
            configured_calendar is not None
            and source_calendar is not None
            and configured_calendar != source_calendar
        ):
            raise ValueError(
                f"{symbol!r} market-data source calendar_id={source_calendar!r} conflicts "
                f"with configured calendar_id={configured_calendar!r}"
            )
        calendars[symbol] = configured_calendar or source_calendar
    return calendars


def _resolve_market_data_subscriptions(
    timeframe: str,
    session_mode: MarketDataSessionMode,
    instruments: Mapping[str, SymbolInfo],
    calendar_ids: Mapping[str, str | None],
) -> dict[str, MarketDataSubscription]:
    """Build exact subscriptions from the already-resolved source calendars."""
    subscriptions: dict[str, MarketDataSubscription] = {}
    for symbol, instrument in instruments.items():
        calendar_id = calendar_ids.get(symbol)
        if calendar_id is None:
            raise ValueError(
                f"market-data source for {symbol!r} must declare market_data_calendar_id "
                "when the instrument route has no calendar_id"
            )
        subscriptions[symbol] = subscription_from_instrument(
            instrument,
            timeframe=timeframe,
            session_mode=session_mode,
            calendar_id=calendar_id,
        )
    return subscriptions


def _resolve_market_data_subscription_snapshot(
    timeframe: str,
    session_mode: MarketDataSessionMode,
    instruments: Mapping[str, SymbolInfo],
    source_capabilities: Mapping[str, _MarketDataSourceCapabilities],
    *,
    default_route_owners: Mapping[str, str | None] | None = None,
) -> _MarketDataSubscriptionSnapshot:
    """Read source capabilities once and resolve one immutable runtime identity."""
    defaults = default_route_owners or {}
    route_owners = {
        symbol: (
            source_capabilities[symbol].route_owner
            if symbol in source_capabilities
            else defaults.get(symbol)
        )
        for symbol in instruments
    }
    effective_calendars = _resolve_effective_calendars_from_capabilities(
        instruments,
        source_capabilities,
    )
    _validate_market_data_calendar_preconditions(
        timeframe,
        instruments,
        route_owners,
        effective_calendars,
    )
    subscriptions = _resolve_market_data_subscriptions(
        timeframe,
        session_mode,
        instruments,
        effective_calendars,
    )
    return _MarketDataSubscriptionSnapshot(route_owners, subscriptions)


def _validate_market_data_calendar_preconditions(
    timeframe: str,
    instruments: Mapping[str, SymbolInfo],
    route_owners: Mapping[str, str | None],
    calendar_ids: Mapping[str, str | None],
) -> None:
    """Fail before polling when a route cannot normalize its requested bars.

    WHY the rule names a route here rather than living in its adapter: an
    adapter cannot refuse a run it has not been constructed for, and this
    has to fail at startup rather than on the first daily bar. The route
    name is hardcoded, so the engine does carry that much venue knowledge;
    what keeps it defensible is that it keys on a name the caller declared,
    and that the rule governs bar normalization the engine performs rather
    than the venue's own trading rules. Every other route is left alone.
    """
    from librae.core.utils import to_ccxt

    if to_ccxt(timeframe) != "1d":
        return
    missing = sorted(
        symbol
        for symbol in instruments
        if route_owners.get(symbol) == "ibkr" and calendar_ids.get(symbol) is None
    )
    if missing:
        raise ValueError(
            "daily IBKR market data requires calendar_id for every IBKR-routed "
            f"symbol; missing {missing}"
        )
    for symbol in instruments:
        if route_owners.get(symbol) != "ibkr":
            continue
        calendar_id = calendar_ids[symbol]
        try:
            validate_calendar_id(calendar_id)
        except ValueError as exc:
            raise ValueError(
                f"daily IBKR market data has invalid calendar_id for {symbol!r}: {calendar_id!r}"
            ) from exc


def _is_recovery_exit(request: OrderRequest) -> bool:
    """Whether ``request`` is an engine-owned drawdown or operator-flatten exit."""
    return request.reason in (
        REASON_DRAWDOWN_BREACH,
        REASON_OPERATOR_FLATTEN,
    ) and request.position_effect in ("reduce", "close")


class ReportNotAppliedError(RuntimeError):
    """A venue answer about a tracked order did not reconcile with it; its cancel went ahead."""

    def __init__(self, message: str, order: TrackedOrder, *, content: str) -> None:
        super().__init__(message)
        self.order = order
        # What makes a later failure the same problem to alert on: the
        # mismatch, without what became of its cancel, which can carry a
        # venue's error text that differs on every attempt.
        self.content = content


def _report_failure(request: OrderRequest, order_id: str, error: Exception) -> str:
    return (
        f"{request.symbol} order_id={order_id or 'unassigned'} broker report not applied "
        f"({type(error).__name__}: {error})"
    )


def _timeout_message(tracked: TrackedOrder) -> str:
    return (
        f"{tracked.request.symbol} order_id={tracked.order_id} status={tracked.status} "
        f"filled={tracked.filled_quantity:.4f}/{tracked.request.quantity:.4f}"
    )


class LiveTrader:
    """Polling-based runner for sim/live modes.

    Args:
        strategy: Strategy instance (same as backtest).
        feature_fn: Legacy per-symbol callable with entry_signal/exit_signal.
            Exactly one of ``feature_fn`` and ``batch_feature_fn`` is required.
        batch_feature_fn: Explicit cross-asset callback evaluated once per
            committed primary cohort.
        config: RunConfig — the sole configuration source.
        adapter: Callable bar fetcher, concrete adapter with ``fetch_ohlcv``,
            or per-symbol mapping. Required. Extra point-in-time columns reach
            ``feature_fn`` except reserved ``available_at``, which remains on
            the audit/persistence view only.
        order_adapter: Required broker gateway in live mode; unused in sim.
        cost_model: CostModel override. None resolves one model per symbol.
        callbacks: Optional analytics hooks. They have no default persistence
            implementation inside the engine.
        warmup_fetcher: Optional data-layer warmup hook; otherwise the injected
            market-data adapter is used.
        state_store: Optional checkpoint store. Live mode requires a durable
            store so placement attempts and fills survive process restarts.
        _market_data_snapshot: Internal deployment-factory handoff for an
            already-resolved source capability and subscription snapshot.
        runtime_revision: Caller-owned opaque runtime identity. Live mode
            requires it so checkpoints cannot cross code or image revisions.
        execution_identity: Optional factory-observed broker identity. In live
            mode the order adapter is still queried and must report the exact
            same value before checkpoint lookup.
        notifier: Optional operational notifier implementing ``Notifier``.
        status_interval_periods: Optional polling-period cadence for status
            notifications. Scheduling is separate from the transport.
        on_ready: Optional deployment hook called after state restoration,
            durable ownership, and startup broker reconciliation.
        on_run_registered: Optional hook called with the resolved run_id on
            every construction, restored or not, for a caller whose
            state_store enforces a run must be registered first (e.g. a
            foreign key to a run-metadata table). On a fresh run it fires
            before the first durable checkpoint write; on a restored run it
            fires inside state restoration, before the state_recovered
            runtime event, so callers that cache run_id from this call alone
            (e.g. to stamp later DB writes) are in sync before any event can
            reach them — no other callback receives run_id as an argument.
    """

    def __init__(
        self,
        strategy: Strategy,
        feature_fn: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
        *,
        config: RunConfig,
        batch_feature_fn: BatchFeatureFn | None = None,
        adapter: object | Mapping[str, object] | None = None,
        order_adapter: object | Mapping[str, object] | None = None,
        cost_model: CostModel | Mapping[str, CostModel] | None = None,
        notifier: Notifier | None = None,
        status_interval_periods: int | None = None,
        on_bar: BarCallback | None = None,
        on_position_event: PositionEventCallback | None = None,
        on_ohlcv: OhlcvCallback | None = None,
        on_ohlcv_batch: OhlcvBatchCallback | None = None,
        on_heartbeat: HeartbeatCallback | None = None,
        on_signal_outcome: SignalOutcomeCallback | None = None,
        on_financing_cash_flow: FinancingCashFlowCallback | None = None,
        on_runtime_event: RuntimeEventCallback | None = None,
        on_performance: PerformanceCallback | None = None,
        on_ready: Callable[[str], None] | None = None,
        on_run_registered: Callable[[str], None] | None = None,
        warmup_fetcher: WarmupFetcher | None = None,
        state_store: LiveStateStore | None = None,
        _market_data_snapshot: _MarketDataSubscriptionSnapshot | None = None,
        runtime_revision: str | None = None,
        execution_identity: ExecutionIdentity | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        from librae.config.symbols import resolve_symbol
        from librae.core.cost_model import CostModel
        from librae.core.utils import generate_run_id, interval_to_timedelta, to_ccxt

        if (feature_fn is None) == (batch_feature_fn is None):
            raise ValueError("exactly one of feature_fn and batch_feature_fn is required")
        if on_ohlcv_batch is not None and on_ohlcv is None:
            raise ValueError("on_ohlcv_batch is the many-bar path of an on_ohlcv sink; pass both")
        if feature_fn is not None and not callable(feature_fn):
            raise TypeError("feature_fn must be callable or None")
        if batch_feature_fn is not None and not callable(batch_feature_fn):
            raise TypeError("batch_feature_fn must be callable or None")
        self._strategy = strategy
        self._feature_fn = feature_fn
        self._batch_feature_fn = batch_feature_fn
        self._config = config
        self._symbols = config.symbols
        self._timeframe = to_ccxt(config.timeframe)
        self._interval_delta = interval_to_timedelta(self._timeframe)
        self._poll_seconds = config.runtime.poll_seconds
        # A bar the venue has not closed by one poll waits for the next, so a
        # poll as long as the bar can leave every decision a bar late.
        if self._poll_seconds > 0 and self._poll_seconds >= self._interval_delta.total_seconds():
            logger.warning(
                "poll_seconds=%s is not shorter than timeframe=%s (%s seconds); "
                "runtime polling may observe bars late",
                self._poll_seconds,
                config.timeframe,
                int(self._interval_delta.total_seconds()),
            )
        self._reconciliation_interval_seconds = config.runtime.reconciliation_interval_seconds
        self._market_data_workers = config.runtime.market_data_workers
        self._clock = clock or (lambda: datetime.now(UTC))
        self._live_order_timeout_seconds = config.execution.live_order_timeout_seconds
        self._max_rebalance_delay_bars = config.execution.max_rebalance_delay_bars
        self._runtime_revision = normalize_runtime_revision(
            runtime_revision,
            required=config.mode == "live",
        )

        # --- Build per-symbol cost models and instrument routes ---
        if isinstance(cost_model, Mapping):
            missing = set(self._symbols) - set(cost_model)
            if missing:
                raise ValueError(f"Missing cost models for symbols: {sorted(missing)}")
            resolved_cost_models = dict(cost_model)
        elif cost_model is not None:
            resolved_cost_models = {symbol: cost_model for symbol in self._symbols}
        else:
            resolved_cost_models = {
                symbol: CostModel.from_config(config, symbol=symbol) for symbol in self._symbols
            }
        self._instruments = {
            symbol: resolve_symbol(
                config,
                symbol,
                multiplier=resolved_cost_models[symbol].multiplier,
            )
            for symbol in self._symbols
        }
        self._account_id = config.account_id
        self._currency = config.account.currency

        # --- Bind caller-owned market-data adapters ---
        if adapter is None:
            raise ValueError(
                "LiveTrader requires an explicit market-data adapter; use "
                "librae.orchestration.live.build_live_trader() for built-in wiring"
            )
        if isinstance(adapter, Mapping):
            missing = set(self._symbols) - set(adapter)
            if missing:
                raise ValueError(f"Missing market-data adapters for symbols: {sorted(missing)}")
            sources = dict(adapter)
        else:
            sources = {symbol: adapter for symbol in self._symbols}
        snapshot = _market_data_snapshot
        if snapshot is None:
            snapshot = _resolve_market_data_subscription_snapshot(
                self._timeframe,
                config.session_mode,
                self._instruments,
                _read_market_data_source_capabilities(sources),
            )
        expected_symbols = set(self._symbols)
        if (
            set(snapshot.route_owners) != expected_symbols
            or set(snapshot.subscriptions) != expected_symbols
        ):
            raise ValueError("market-data snapshot symbols must exactly match config.symbols")
        _validate_market_data_calendar_preconditions(
            config.timeframe,
            self._instruments,
            snapshot.route_owners,
            {
                symbol: subscription.calendar_id
                for symbol, subscription in snapshot.subscriptions.items()
            },
        )
        self._optional_symbols = frozenset(config.optional_symbols)
        self._market_data_subscriptions = dict(snapshot.subscriptions)
        # Auxiliary inputs reuse their symbol's resolved instrument, calendar
        # and data route, so they need no separate resolution or source
        # binding — only a different timeframe through the same fetcher.
        self._auxiliary_subscriptions: dict[MarketDataSubscription, str] = {}
        for auxiliary in config.auxiliary_subscriptions:
            subscription = subscription_from_instrument(
                self._instruments[auxiliary.symbol],
                timeframe=auxiliary.timeframe,
                session_mode=config.session_mode,
                calendar_id=snapshot.subscriptions[auxiliary.symbol].calendar_id,
            )
            self._auxiliary_subscriptions[subscription] = auxiliary.symbol
        self._auxiliary_cache: dict[MarketDataSubscription, pd.DataFrame] = {}
        self._primary_subscriptions = tuple(
            self._market_data_subscriptions[symbol] for symbol in self._symbols
        )
        if config.execution.adv_lookback_sessions is not None and self._interval_delta.days < 1:
            for subscription in self._market_data_subscriptions.values():
                validate_calendar_id(subscription.calendar_id)
        self._fetchers = {
            symbol: _bind_market_data_source(
                sources[symbol],
                self._instruments[symbol],
                config.session_mode,
                route_owner=snapshot.route_owners[symbol],
                calendar_id=self._market_data_subscriptions[symbol].calendar_id,
            )
            for symbol in self._symbols
        }

        if config.mode == "live":
            if isinstance(order_adapter, Mapping):
                order_adapters = dict(order_adapter)
            elif order_adapter is not None:
                order_adapters = {symbol: order_adapter for symbol in self._symbols}
            else:
                raise ValueError(
                    "live mode requires an explicit order_adapter; use "
                    "librae.orchestration.live.build_live_trader() for built-in wiring"
                )
            missing = set(self._symbols) - set(order_adapters)
            if missing:
                raise ValueError(f"Missing order adapters for symbols: {sorted(missing)}")
            if len({id(route) for route in order_adapters.values()}) != 1:
                raise ValueError(
                    "one live run owns one account and requires one shared order "
                    "adapter (why: docs/decisions/2026-09-09-one-run-owns-one-execution-venue.md)"
                )
        else:
            order_adapters = {}

        is_live = config.mode == "live"
        if is_live:
            shared_order_adapter = next(iter(order_adapters.values()))
            observed_execution_identity = resolve_execution_identity(shared_order_adapter)
            if execution_identity is not None and execution_identity != observed_execution_identity:
                raise RuntimeError(
                    "declared and adapter-observed execution identities disagree; "
                    "trading did not start"
                )
            self._execution_identity = observed_execution_identity
            logger.info("Live execution identity: %s", self._execution_identity.summary)
            # Optional: an adapter that cannot read these venue facts starts unchecked.
            verify_venue_setup = getattr(shared_order_adapter, "verify_venue_setup", None)
            if callable(verify_venue_setup):
                verify_venue_setup(self._instruments)
        else:
            if execution_identity is not None:
                raise ValueError("execution_identity is only valid in live mode")
            self._execution_identity = None

        # --- Build run_id ---
        strategy_name = config.strategy_name
        self._run_id = generate_run_id(
            f"{strategy_name}_{config.market}",
            config.symbol,
            config.timeframe,
        )

        # --- Build executor ---
        self._executor = LiveExecutor(
            resolved_cost_models,
            simulation=not is_live,
            strategy_name=strategy_name,
            order_adapter=order_adapters if is_live else None,
            instruments=self._instruments,
        )

        # --- Restore restart-critical state before callbacks capture run_id ---
        self._state_key = runtime_state_key(
            config.mode,
            config.config_hash,
            self._execution_identity,
        )
        self._account_lease_key = (
            account_lease_key(self._execution_identity)
            if self._execution_identity
            else f"sim-account:{self._account_id}"
        )
        self._state_store = state_store
        if is_live and self._state_store is None:
            raise ValueError("live mode requires an explicit durable state_store")
        if is_live and getattr(self._state_store, "restart_durable", False) is not True:
            # Fail closed on silence, and on anything that is not literally
            # True: the persistence methods alone are satisfied by a
            # dictionary, so an undeclared store would reach order-capable
            # startup with no recovery state and only reveal it after a crash.
            # A truthy string or 1 is not the bool claim the Protocol types.
            raise ValueError(
                f"live mode requires a restart-durable state_store; "
                f"{type(self._state_store).__name__} does not declare "
                "restart_durable=True. Use the reference database store, or a "
                "custom store that genuinely survives the process."
            )

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        # When this process first received each cached row version, keyed by
        # (ts, available_at) in nanoseconds. Kept beside the cache rather than
        # as a column so strategies never see it; it feeds only the audit row.
        self._bar_observed_at: dict[str, dict[tuple[int, int], datetime]] = {}
        self._consecutive_errors: int = 0
        self._market_data_fetch_failures: dict[str, int] = {}
        # Recent fetch health is intentionally process-local. Persisting a
        # short operational window across downtime would mix unlike polling
        # cadences and can raise a stale alert after an otherwise clean
        # restart; it is diagnostic state, not execution state.
        self._market_data_fetch_history: dict[str, deque[bool]] = {}
        self._market_data_fetch_degraded: set[str] = set()
        self._last_cycle_ts: datetime | None = None
        self._last_execution_bar_ts: dict[str, datetime] = {}
        self._execution_bar_filled_quantities: dict[str, float] = {}
        self._last_feature_as_of: datetime | None = None
        self._last_bar_ts: dict[str, datetime] = {}
        self._last_financing_ts: dict[str, datetime] = {}
        self._stale_alerted: dict[MarketDataSubscription, bool] = {}
        self._data_gap_alerted = False
        self._auxiliary_never_delivered: set[MarketDataSubscription] = set()
        self._unanchored_reported: set[MarketDataSubscription] = set()
        self._last_prices: dict[str, float] = {}
        self._positions: dict[str, PositionState] = {}
        self._cash = config.account.initial_cash
        self._halted: bool = False
        # Operator pause: orders that open or add are dropped, exits still go out.
        self._entries_paused: bool = False
        # A halt the store has not recorded: a restart would resume unhalted.
        self._halt_unpersisted = False
        # Whether a halt still owes the live cancellation it skipped, that failed,
        # or that the venue has not yet confirmed for every tracked order.
        self._halt_cancels_pending = False
        # Each poll cycle and each synchronous operator halt is one pass over
        # the tracked orders, and a cancel goes out at most once per order in
        # a pass: a status read just after it may not show it yet.
        self._order_pass = 0
        # The pass that last sent a cancel the venue answered, by client order
        # id. Beyond that pass, a cancel is held back only while the venue
        # reports it pending; a pending state the venue set itself was never
        # ours. Not checkpointed, so after a restart a pending cancel goes out
        # once more.
        self._cancels_sent: dict[str, int] = {}
        # Alerts that would otherwise repeat every cycle while the venue's
        # answers about an order do not apply, keyed by client order id, or
        # while a flatten waits for the venue to confirm its cancels.
        self._report_alerts = PersistentAlerts(self._send_alert, self._utc_now)
        self._flatten_alert = PersistentAlerts(self._send_alert, self._utc_now)
        # The order pass of each tracked order's latest unappliable answer,
        # alerted or not.
        self._report_failed_pass: dict[str, int] = {}
        # Consecutive cycles a pending flatten found orders still tracked
        # after its cancels. Venues that confirm a cancel a cycle later make
        # the first one normal.
        self._flatten_waits = 0
        # What a failed checkpoint left unwritten, keyed by client order id, and
        # the fills it left unpublished; the next checkpoint that lands carries both.
        self._unpersisted_orders: dict[str, TrackedOrder] = {}
        self._unpublished_results: list[ExecutionResult] = []
        self._order_ledger_degraded = False
        self._pending_decision: StrategyDecision = []
        # Bar timestamp each pending intent first rested on, keyed by symbol.
        # A resting "day" limit expires against the session it first rested in.
        self._pending_resting_since: dict[str, datetime] = {}
        self._active_orders: list[TrackedOrder] = []
        self._live_rebalance: LiveRebalance | None = None
        self._equity_peak = self._cash
        self._prev_equity = self._cash
        self._status_window_equity = self._cash
        self._trade_count: int = 0
        self._event_sequence: int = 0
        self._performance_dirty: bool = False
        self._pending_traded_notional = 0.0
        self._portfolio_diagnostics = (0.0, 0.0, 0.0, 0.0)
        self._period_index: int = 0
        self._status_period_count: int = 0
        self._adv_session_labels: dict[str, str] = {}
        self._adv_filled_quantities: dict[str, float] = {}
        self._last_reconciliation_at: datetime | None = None
        self._resume_unverified = False
        self._reconciliation_unavailable_rounds = 0
        self._reconciliation_history: deque[bool] = deque(maxlen=self.FETCH_HEALTH_WINDOW)
        self._reconciliation_degraded = False
        self._skipped_close_alerts: dict[tuple[str, str], float] = {}
        # Not checkpointed: a restart re-alerts once.
        self._unfunded_entry_alerts: set[tuple[str, str]] = set()
        # Queued groups that have sent no leg, which the funding and pause
        # checks still judge. Checkpointed: a restart must neither send an
        # unjudged group nor re-judge a started one.
        self._unstarted_groups: set[str] = set()
        # Set only while a flatten submits its exits, before it halts the account.
        self._flatten_at: datetime | None = None
        # The last order an entry fill was booked for, until the risk limits are checked.
        self._entry_fill_unchecked: TrackedOrder | None = None
        # The last bar a target rebalance stopped on; not checkpointed.
        self._rebalance_stopped_at: datetime | None = None
        self._cycle_fetch_seconds: dict[str, float] = {}
        self._cycle_strategy_seconds = 0.0
        self._cycle_order_seconds = 0.0
        self._last_cycle_diagnostics: CycleDiagnostics | None = None
        self._warmup_requested_periods: dict[str, int] = {}
        self._warmup_fetch_attempts: dict[str, int] = {}
        # Polls left until the next largest-request re-probe, and the interval.
        self._warmup_exhausted: dict[str, tuple[int, int]] = {}
        self._replay_backlog_exhausted: dict[str, tuple[int, int]] = {}
        self._reported_warmup_reasons: dict[str, str] = {}
        self._lease_acquired = False
        self._account_lease_acquired = False
        self._restored_state = False
        self._on_runtime_event = on_runtime_event
        # Before the restore, which refills it from the checkpoint.
        self._pending_ohlcv: list[PendingOhlcvDelivery] = []
        if self._state_store is not None:
            restored = self._state_store.load(self._state_key)
            if restored is not None:
                self._restore_state(restored, on_run_registered)

        configured_warmup = config.execution.warmup_periods
        self._feature_history_limit = configured_warmup
        adv_warmup = (config.execution.adv_lookback_sessions or 0) + 1
        self._warmup_periods = max(configured_warmup, adv_warmup)

        self._on_bar = on_bar
        self._on_position_event = on_position_event
        # Opt-in like OHLCV delivery below: only a sink that declares it is
        # idempotent may be offered the same event again.
        position_sink = getattr(on_position_event, "__self__", on_position_event)
        self._durable_position_event_delivery = (
            getattr(position_sink, "durable_position_event_delivery", False) is True
        )
        self._pending_position_events: list[tuple[PositionEvent, int]] = []
        self._position_event_delivery_degraded = False
        # Poll cycles in which the head of the queue was refused in a row.
        self._head_position_event_failures = 0
        # Poll cycles in a row whose flush drained the queue with nothing refused.
        self._clean_position_event_cycles = 0
        self._dropped_position_event_sequences: list[int] = []
        self._on_ohlcv = on_ohlcv
        self._on_ohlcv_batch = on_ohlcv_batch
        # A sink opts into at-least-once delivery by declaring it. Anything
        # else keeps the documented best-effort contract, because the engine
        # cannot acknowledge on a caller's behalf.
        sink_owner = getattr(on_ohlcv, "__self__", on_ohlcv)
        self._durable_ohlcv_delivery = getattr(sink_owner, "durable_ohlcv_delivery", False) is True
        self._ohlcv_audit_degraded = False
        self._on_heartbeat = on_heartbeat
        self._on_signal_outcome = on_signal_outcome
        self._on_financing_cash_flow = on_financing_cash_flow
        self._on_performance = on_performance
        self._on_ready = on_ready
        self._warmup_fetcher = warmup_fetcher
        self._notifier = notifier

        self._fill_price = config.execution.default_fill_price
        self._max_bar_volume_participation_rate = config.execution.max_bar_volume_participation_rate
        self._adv_lookback_sessions = config.execution.adv_lookback_sessions
        self._max_adv_participation_rate = config.execution.max_adv_participation_rate
        self._risk_policy = config.risk
        self._running: bool = False

        if status_interval_periods is not None and (
            isinstance(status_interval_periods, bool)
            or not isinstance(status_interval_periods, int)
            or status_interval_periods <= 0
        ):
            raise ValueError("status_interval_periods must be a positive integer or None")
        if status_interval_periods is not None and notifier is None:
            raise ValueError("status_interval_periods requires a notifier")
        self._status_interval = status_interval_periods

        self._notify_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")
        # Wakes the sleep between cycles. SimpleQueue.put is reentrant, so a
        # signal handler may call it even while it interrupts a get or put on
        # this thread; Event.set takes a lock the interrupted code may hold.
        self._wake: SimpleQueue[None] = SimpleQueue()
        self._sleep = self._wait_for_next_cycle  # instance attribute so tests can skip real delays
        # Held for each cycle: an operator call from another thread waits for
        # the cycle in progress and applies between cycles.
        self._cycle_lock = RLock()
        # Set once run() has shut down; operator calls then raise instead of
        # mutating a run that no longer owns the account.
        self._closed = False
        # Requests another thread or a signal records; the loop acts on them.
        # Halt and halt reset requests stay lock-free so a signal handler,
        # which interrupts the loop thread mid-cycle, can never block on a
        # lock that thread holds.
        self._halt_requests: SimpleQueue[str] = SimpleQueue()
        self._halt_reset_requests: SimpleQueue[None] = SimpleQueue()
        self._flatten_request_lock = Lock()
        self._flatten_request: tuple[int, str] | None = None
        self._flatten_request_sequence = 0
        if not self._restored_state:
            # A restored run already notified on_run_registered inside
            # _restore_state, before it fires the state_recovered event —
            # callers like _TimescaleCallbacks cache run_id from this call
            # alone and have no other way to learn it, since on_bar is the
            # only other callback that receives run_id as an argument rather
            # than reading cached state.
            if on_run_registered is not None:
                on_run_registered(self._run_id)
            self._persist_state()

    # --- Durable runtime state ---

    def _cash_for_symbol(self, symbol: str) -> float:
        return self._cash

    def _record_resting_since(
        self,
        decision: StrategyDecision,
        ts: datetime,
        *,
        primary_symbol: str,
    ) -> None:
        resting_orders.record_resting_since(
            decision, ts, self._pending_resting_since, primary_symbol=primary_symbol
        )

    def _replace_resting_intents(
        self,
        new_decision: StrategyDecision,
        ts: datetime,
        *,
        primary_symbol: str,
    ) -> None:
        self._pending_decision = resting_orders.replace_resting_intents(
            self._pending_decision,
            new_decision,
            ts,
            self._pending_resting_since,
            primary_symbol=primary_symbol,
            on_runtime_event=self._on_runtime_event,
        )

    def _validate_resting_sessions(
        self,
        decision: StrategyDecision,
        ts: datetime,
        *,
        primary_symbol: str,
    ) -> None:
        del ts
        resting_orders.validate_resting_sessions(
            decision,
            primary_symbol=primary_symbol,
            instruments=self._instruments,
            timeframe=self._config.timeframe,
        )

    def _prune_pending_submissions(self, *, primary_symbol: str) -> None:
        self._pending_resting_since = resting_orders.prune_pending_submissions(
            self._pending_decision,
            self._pending_resting_since,
            primary_symbol=primary_symbol,
        )

    def _session_of(self, symbol: str, ts: datetime) -> object:
        return resting_orders.session_of(
            symbol,
            ts,
            calendar_id=self._instruments[symbol].calendar_id,
            timeframe=self._config.timeframe,
        )

    def _expire_resting_day_intents(
        self,
        ts: datetime,
        priced_symbols: Container[str],
        *,
        primary_symbol: str,
    ) -> None:
        self._pending_decision = resting_orders.expire_resting_day_intents(
            self._pending_decision,
            ts,
            priced_symbols,
            self._pending_resting_since,
            primary_symbol=primary_symbol,
            session_of=self._session_of,
            on_runtime_event=self._on_runtime_event,
        )

    def _without_halted_account(self, decision: StrategyDecision) -> StrategyDecision:
        return [] if self._halted else decision

    def _snapshot_state(self) -> LiveRuntimeState:
        return LiveRuntimeState(
            state_key=self._state_key,
            run_id=self._run_id,
            config_hash=self._config.config_hash,
            mode=self._config.mode,
            account_id=self._account_id,
            execution_identity=self._execution_identity,
            runtime_revision=self._runtime_revision,
            cash=self._cash,
            positions=deepcopy(self._positions),
            last_prices=dict(self._last_prices),
            last_cycle_ts=self._last_cycle_ts,
            last_execution_bar_ts=dict(self._last_execution_bar_ts),
            execution_bar_filled_quantities=dict(self._execution_bar_filled_quantities),
            last_feature_as_of=self._last_feature_as_of,
            last_bar_ts=dict(self._last_bar_ts),
            last_financing_ts=dict(self._last_financing_ts),
            pending_decision=deepcopy(self._pending_decision),
            pending_resting_since=dict(self._pending_resting_since),
            pending_ohlcv=list(self._pending_ohlcv),
            active_orders=deepcopy(self._active_orders),
            live_rebalance=deepcopy(self._live_rebalance),
            equity_peak=self._equity_peak,
            prev_equity=self._prev_equity,
            status_window_equity=self._status_window_equity,
            trade_count=self._trade_count,
            event_sequence=self._event_sequence,
            period_index=self._period_index,
            status_period_count=self._status_period_count,
            halted=self._halted,
            entries_paused=self._entries_paused,
            unstarted_groups=sorted(self._unstarted_groups),
            adv_session_labels=dict(self._adv_session_labels),
            adv_filled_quantities=dict(self._adv_filled_quantities),
        )

    def _restore_state(
        self, state: LiveRuntimeState, on_run_registered: Callable[[str], None] | None
    ) -> None:
        if state.state_key != self._state_key:
            raise ValueError("runtime state key does not match this configuration")
        if state.config_hash != self._config.config_hash or state.mode != self._config.mode:
            raise ValueError("runtime state configuration does not match this run")
        if not self._executor.simulation and state.runtime_revision != self._runtime_revision:
            raise RuntimeError(
                "live checkpoint runtime revision mismatch: "
                f"checkpoint={state.runtime_revision!r}, requested={self._runtime_revision!r}; "
                "select the matching runtime revision or perform an explicit "
                "checkpoint migration or flat-account reset"
            )
        self._run_id = state.run_id
        if state.account_id != self._account_id:
            raise ValueError("runtime state account does not match this run")
        if state.execution_identity != self._execution_identity:
            raise RuntimeError("runtime state execution identity does not match this broker route")
        # A checkpoint bypasses decision preflight, and one written by an
        # engine that let simulation short crypto spot may still queue one.
        # A held short is left alone: closing it is a buy.
        restored_decisions = [("pending decision", state.pending_decision)]
        if state.live_rebalance is not None:
            restored_decisions.append(("live rebalance", state.live_rebalance.targets))
        for label, decision in restored_decisions:
            refused = refused_short_symbols(
                decision, primary_symbol=self._symbols[0], can_short=self._can_short
            )
            if refused:
                raise ValueError(
                    f"runtime checkpoint {label} shorts {refused}, which cannot open or add "
                    "to a short (crypto spot sells owned inventory); perform an explicit "
                    "checkpoint migration that removes it before resuming"
                )
        self._cash = state.cash
        self._positions = state.positions
        self._last_prices = state.last_prices
        self._last_cycle_ts = state.last_cycle_ts
        self._last_execution_bar_ts = state.last_execution_bar_ts
        self._execution_bar_filled_quantities = state.execution_bar_filled_quantities
        self._last_feature_as_of = state.last_feature_as_of
        self._last_bar_ts = state.last_bar_ts
        self._last_financing_ts = state.last_financing_ts
        self._pending_decision = state.pending_decision
        self._pending_resting_since = dict(state.pending_resting_since)
        self._pending_ohlcv = list(state.pending_ohlcv)
        self._active_orders = state.active_orders
        self._unstarted_groups = set(state.unstarted_groups)
        self._live_rebalance = state.live_rebalance
        self._equity_peak = state.equity_peak
        self._prev_equity = state.prev_equity
        self._status_window_equity = state.status_window_equity
        self._trade_count = state.trade_count
        self._event_sequence = state.event_sequence
        self._period_index = state.period_index
        self._status_period_count = state.status_period_count
        self._halted = state.halted
        self._entries_paused = state.entries_paused
        self._adv_session_labels = state.adv_session_labels
        self._adv_filled_quantities = state.adv_filled_quantities
        # The flag is not checkpointed, so a stop between a close and its
        # refresh would leave the stored KPIs behind the stored trades.
        self._performance_dirty = True
        self._restored_state = True
        logger.info(
            "Restored runtime state: key=%s run_id=%s cycle=%s orders=%d halted=%s "
            "entries_paused=%s",
            self._state_key,
            self._run_id,
            self._last_cycle_ts,
            len(self._active_orders),
            self._halted,
            self._entries_paused,
        )
        # Notify callbacks of the restored run_id before firing any event —
        # state_recovered below fires synchronously, and callers like
        # _TimescaleCallbacks cache run_id from on_run_registered alone.
        if on_run_registered is not None:
            on_run_registered(self._run_id)
        if self._on_runtime_event:
            self._on_runtime_event(
                RuntimeEvent(
                    ts=self._clock(),
                    event_type="state_recovered",
                    detail={
                        "last_cycle_ts": (
                            self._last_cycle_ts.isoformat() if self._last_cycle_ts else None
                        ),
                        "active_orders": len(self._active_orders),
                        "halted": self._halted,
                        "entries_paused": self._entries_paused,
                    },
                )
            )

    def _persist_state(self, *orders: TrackedOrder) -> None:
        """Critical checkpoint write; failures propagate and stop the cycle.

        Order rows a failed write did not land ride along with the next one,
        in the same transaction as the snapshot, because an order that went
        terminal meanwhile is no longer in the snapshot to rewrite it.

        A failed write that carried rows is retried with the snapshot alone.
        If that lands, the store is up and a row was refused, so each row is
        written on its own and one the store still refuses is dropped loudly:
        a carried row the store never accepts would otherwise fail every
        later checkpoint, including a halt's, and its cancellation with it.
        """
        if self._state_store is None:
            return
        for tracked in orders:
            self._unpersisted_orders[tracked.request.client_order_id] = tracked
        snapshot = self._snapshot_state()
        try:
            self._state_store.save(snapshot, tuple(self._unpersisted_orders.values()))
        except Exception as exc:
            if not self._unpersisted_orders or not self._store_accepts(snapshot):
                self._log_checkpoint_failure(exc)
                raise
            self._write_rows_one_by_one(snapshot)
        self._unpersisted_orders.clear()

    def _store_accepts(self, snapshot: LiveRuntimeState) -> bool:
        """Write the snapshot with no rows; False means the store itself is failing."""
        try:
            self._state_store.save(snapshot, ())
        except Exception:
            return False
        return True

    def _write_rows_one_by_one(self, snapshot: LiveRuntimeState) -> None:
        """Write each carried row alone, dropping only one the store refuses while up.

        A row is tried twice, so a connection lost mid-write does not read as
        a refusal, and its second failure is re-checked with the snapshot
        alone, so a store that goes down partway keeps the rest carried and
        raises, as an outage does.
        """
        for client_order_id, tracked in list(self._unpersisted_orders.items()):
            try:
                self._save_row(snapshot, tracked)
            except Exception as exc:
                if not self._store_accepts(snapshot):
                    self._log_checkpoint_failure(exc)
                    raise
                del self._unpersisted_orders[client_order_id]
                self._reject_order_row(tracked, exc)
                continue
            del self._unpersisted_orders[client_order_id]

    def _save_row(self, snapshot: LiveRuntimeState, tracked: TrackedOrder) -> None:
        try:
            self._state_store.save(snapshot, (tracked,))
        except Exception:
            self._state_store.save(snapshot, (tracked,))

    def _log_checkpoint_failure(self, exc: Exception) -> None:
        logger.error(
            "Checkpoint not written (%s); order rows pending: %s",
            exc,
            ", ".join(self._unpersisted_orders) or "none",
        )

    def _reject_order_row(self, tracked: TrackedOrder, exc: Exception) -> None:
        """Drop one order row the store refuses, without stopping the book.

        The broker still holds the order, but a terminal order has left the
        checkpoint, so its ledger row keeps an older state and the runtime
        event is the only record of the refused one. The flag and alert come
        before that event, so a failing event sink cannot hide the gap.
        """
        request = tracked.request
        logger.error(
            "Order row %s (%s) refused by the state store (%s: %s); dropped",
            request.client_order_id,
            tracked.status,
            type(exc).__name__,
            exc,
        )
        if not self._order_ledger_degraded:
            self._order_ledger_degraded = True
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Order Row Rejected",
                message=(
                    f"The state store refused the broker_orders row of "
                    f"{request.client_order_id} ({tracked.status}; {type(exc).__name__}: "
                    f"{exc}); it was dropped and each refused row is recorded as a runtime "
                    "event. Trading continues; the ledger may show an older state for it "
                    "until the row is restored."
                ),
            )
        if self._on_runtime_event:
            self._on_runtime_event(
                RuntimeEvent(
                    ts=self._utc_now(),
                    event_type="decision_skipped",
                    symbol=request.symbol,
                    detail={
                        "reason": "order_row_rejected",
                        "client_order_id": request.client_order_id,
                        "status": tracked.status,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
            )

    def _publish_checkpointed_results(self) -> None:
        """Publish fills the last checkpoint holds, oldest first.

        Called only where order work owns the outcome, never from inside a
        checkpoint: a sink error raised there would read as the checkpoint
        failing, and a halt would report itself unpersisted.
        """
        while self._unpublished_results:
            self._publish_action_results(self._unpublished_results.pop(0))

    # WHY: 3 consecutive errors likely means a persistent issue (API down, DB
    # unreachable), not a transient blip — worth alerting the operator.
    CONSECUTIVE_ERROR_THRESHOLD = 3

    # WHY: a skipped reconciliation round only stretches the interval, so
    # bounding consecutive skips bounds how long broker/local drift can go
    # undetected. Only consecutive skips halt: every answered round runs a full
    # reconcile, so intermittent skips thin coverage without removing it, and
    # the FETCH_HEALTH_* window below alerts on them instead.
    RECONCILIATION_UNAVAILABLE_HALT_ROUNDS = 6

    # A six-poll window catches a sustained two-out-of-three failure pattern
    # without promoting a single transient error. Separate alert and recovery
    # thresholds provide hysteresis so a feed near the boundary does not flap
    # the operator diagnostic.
    FETCH_HEALTH_WINDOW = 6
    FETCH_HEALTH_ALERT_FAILURES = 4
    FETCH_HEALTH_RECOVERY_FAILURES = 2

    # WHY: a completed bar's own timestamp is always ~1 interval behind wall
    # clock even when the feed is perfectly healthy (see _check_staleness) —
    # this is how many *additional* full intervals of no progress are
    # tolerated on top of that before alerting. Live skips stale frames rather
    # than submitting decisions from obsolete market snapshots.
    STALE_DATA_TOLERANCE_BARS = 2

    # How long past a bar's close the symbols that have it wait for a
    # required symbol due the same bar. A venue starts each symbol's next
    # candle at its own time, and an adapter releases a bar only then (58
    # Binance perpetuals took 0.8 to 12.2 seconds after a close on 2026-11-05),
    # so one poll can hold a bar for some symbols and not others. Deciding
    # then would read the rest a bar stale, which a backtest never does. Past
    # the hold the late symbols are decided on their own, a poll later.
    COHORT_HOLD = timedelta(seconds=30)

    # The retry queue is in memory, one event per fill, so this sits far above
    # what a book trades through an outage. Reaching it means the sink keeps
    # refusing something, and the oldest event is dropped so one it never
    # accepts cannot hold back every later one.
    MAX_PENDING_POSITION_EVENTS = 1_000

    # Poll cycles the head event may be refused before it is dropped, so one
    # event the sink never accepts cannot hold back the rest until the bound
    # above. Counted once per cycle, so this spans an hour of 5-second polls
    # and twelve hours of minute polls: long enough to ride out a database
    # restart, while the first refusal already alerts.
    MAX_POSITION_EVENT_ATTEMPTS = 720

    # Clean poll cycles before a degraded episode ends and may alert again,
    # so a sink that fails now and then alerts once rather than every cycle.
    POSITION_EVENT_RECOVERY_CYCLES = 60

    # WHY: a portfolio-level decision (rebalance, multi-symbol entry) can fill
    # many symbols in one cycle — pushing one Telegram message per fill floods
    # the operator's phone. Above this count, fills are summarized in a single
    # digest message instead of sent individually.
    SIGNAL_BATCH_THRESHOLD = 3

    # A broker may translate ``limit`` into a wall-clock duration, so the
    # first request can contain fewer usable observations across closed
    # sessions. 1x/2x/4x covers the common calendar/session-density gap while
    # keeping the startup request burst explicit and bounded.
    WARMUP_MAX_FETCH_ATTEMPTS = 3
    WARMUP_MAX_FETCH_MULTIPLIER = 2 ** (WARMUP_MAX_FETCH_ATTEMPTS - 1)
    # Once the largest request stops returning new history, repeating it every
    # poll re-walks the whole window (many pages on a paginated adapter) for
    # history that rarely changes. Doubling keeps the first re-probes prompt;
    # the cap bounds how long newly available older history goes unseen.
    WARMUP_REPROBE_MAX_INTERVAL_POLLS = 32

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _notify(self, method: str, **kwargs: object) -> None:
        """Submit an operational notification to the background thread pool."""
        if self._notifier is None or not self._notifier.enabled:
            return
        fn = getattr(self._notifier, method)
        self._notify_pool.submit(fn, **kwargs)

    def _check_staleness(self, symbol: str, latest_ts: datetime) -> bool:
        subscription = self._market_data_subscriptions[symbol]
        return data_readiness.check_staleness(
            symbol,
            latest_ts,
            subscription=subscription,
            as_of=self._utc_now(),
            grace=self._staleness_grace(subscription),
            stale_alerted=self._stale_alerted,
            unanchored_reported=self._unanchored_reported,
            notify=self._notify,
            strategy_name=self._executor.strategy_name,
        )

    def _report_data_readiness(self, missing: Sequence[str]) -> bool:
        blocking, self._data_gap_alerted = data_readiness.report_data_readiness(
            missing,
            optional_symbols=self._optional_symbols,
            alerted=self._data_gap_alerted,
            notify=self._notify,
            strategy_name=self._executor.strategy_name,
        )
        return blocking

    def _refresh_auxiliary_cache(self) -> None:
        auxiliary_cache.refresh(
            self._auxiliary_subscriptions,
            self._auxiliary_cache,
            now=self._utc_now(),
            fetchers=self._fetchers,
            history_limit=self._feature_history_limit,
            normalize=self._normalize_runtime_rows,
            never_delivered=self._auxiliary_never_delivered,
            stale_alerted=self._stale_alerted,
            staleness_grace=self._staleness_grace,
            notify=self._notify,
            strategy_name=self._executor.strategy_name,
        )

    def _deliver_ohlcv(self, audit_bars: Mapping[str, dict[str, object]], ts: datetime) -> None:
        ohlcv_audit.deliver(
            audit_bars,
            ts,
            on_ohlcv=self._on_ohlcv,
            durable=self._durable_ohlcv_delivery,
            timeframe=self._timeframe,
            flush=self._flush_pending_ohlcv,
        )

    def _queue_ohlcv(self, audit_bars: Mapping[str, dict[str, object]], ts: datetime) -> None:
        self._ohlcv_audit_degraded = ohlcv_audit.queue(
            audit_bars,
            ts,
            self._pending_ohlcv,
            on_ohlcv=self._on_ohlcv,
            durable=self._durable_ohlcv_delivery,
            subscriptions=self._market_data_subscriptions,
            degraded=self._ohlcv_audit_degraded,
            utc_now=self._utc_now,
            on_runtime_event=self._on_runtime_event,
            notify=self._notify,
            strategy_name=self._executor.strategy_name,
        )

    def _flush_pending_ohlcv(self) -> None:
        self._ohlcv_audit_degraded = ohlcv_audit.flush_pending(
            self._pending_ohlcv,
            on_ohlcv=self._on_ohlcv,
            on_ohlcv_batch=self._on_ohlcv_batch,
            degraded=self._ohlcv_audit_degraded,
            persist=self._persist_state,
        )

    def _staleness_grace(self, subscription: MarketDataSubscription) -> timedelta:
        return data_readiness.staleness_grace(
            subscription, tolerance_bars=self.STALE_DATA_TOLERANCE_BARS
        )

    def _finish_cycle_diagnostics(
        self,
        started_at: datetime,
        started_perf: float,
    ) -> None:
        cycle_seconds = perf_counter() - started_perf
        deadline_missed = self._poll_seconds > 0 and cycle_seconds > self._poll_seconds
        self._last_cycle_diagnostics = CycleDiagnostics(
            started_at=started_at,
            fetch_seconds_by_symbol=tuple(sorted(self._cycle_fetch_seconds.items())),
            strategy_seconds=self._cycle_strategy_seconds,
            order_seconds=self._cycle_order_seconds,
            cycle_seconds=cycle_seconds,
            deadline_missed=deadline_missed,
        )
        log = logger.warning if deadline_missed else logger.debug
        log(
            "Cycle latency: total=%.4fs fetch=%s strategy=%.4fs orders=%.4fs deadline_missed=%s",
            cycle_seconds,
            {key: round(value, 6) for key, value in self._cycle_fetch_seconds.items()},
            self._cycle_strategy_seconds,
            self._cycle_order_seconds,
            deadline_missed,
        )

    def _fetch_runtime_frames(self) -> dict[str, _MarketDataFetchResult]:
        """Fetch every required symbol and report its explicit cycle outcome."""

        def fetch_one(symbol: str) -> _MarketDataFetchResult:
            started = perf_counter()
            audit_rows: list[_OhlcvAuditRow] = []
            corrections: list[_OhlcvAuditRow] = []
            if symbol in self._replay_backlog_exhausted:
                return _MarketDataFetchResult(
                    outcome="terminal",
                    frame=self._ohlcv_cache.get(symbol),
                    elapsed_seconds=perf_counter() - started,
                )
            try:
                frame = self._fetch_with_cache_unchecked(
                    symbol,
                    audit_sink=audit_rows,
                    correction_sink=corrections,
                )
            except Exception as exc:
                frame = self._ohlcv_cache.get(symbol)
                return _MarketDataFetchResult(
                    outcome="error",
                    frame=frame,
                    elapsed_seconds=perf_counter() - started,
                    audit_rows=tuple(audit_rows),
                    corrections=tuple(corrections),
                    error=exc,
                )
            outcome: Literal["success", "terminal"] = (
                "terminal" if symbol in self._replay_backlog_exhausted else "success"
            )
            return _MarketDataFetchResult(
                outcome=outcome,
                frame=frame,
                elapsed_seconds=perf_counter() - started,
                audit_rows=tuple(audit_rows),
                corrections=tuple(corrections),
            )

        if self._market_data_workers == 1 or len(self._symbols) == 1:
            results = {symbol: fetch_one(symbol) for symbol in self._symbols}
        else:
            worker_count = min(self._market_data_workers, len(self._symbols))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {symbol: pool.submit(fetch_one, symbol) for symbol in self._symbols}
                results = {symbol: futures[symbol].result() for symbol in self._symbols}

        def by_version(rows: tuple[_OhlcvAuditRow, ...]) -> list[_OhlcvAuditRow]:
            unique: dict[tuple[int, int], _OhlcvAuditRow] = {}
            for event_ts, audit_bar in rows:
                version = (
                    pd.Timestamp(event_ts).value,
                    pd.Timestamp(audit_bar[AVAILABLE_AT_COLUMN]).value,
                )
                # The last one is what the cache kept: a fetch that merges
                # several responses can revise a bar more than once.
                unique[version] = (event_ts, audit_bar)
            return [unique[version] for version in sorted(unique)]

        corrections_queued = False
        sink_took_burst = True
        reread: list[tuple[str, datetime, dict[str, object]]] = []
        for symbol, result in results.items():
            self._cycle_fetch_seconds[symbol] = result.elapsed_seconds
            if result.outcome == "error":
                assert result.error is not None
                self._record_market_data_fetch_failure(symbol, result.error)
            elif result.outcome == "success":
                self._record_market_data_fetch_success(symbol)
            if self._on_ohlcv is None:
                continue
            # A later version of a bar already decided is what the audit
            # exists to keep, and rare, so it rides the same queue as a
            # decided bar.
            for event_ts, audit_bar in by_version(result.corrections):
                if (
                    self._durable_ohlcv_delivery
                    and sink_took_burst
                    and len(self._pending_ohlcv) >= MAX_PENDING_OHLCV
                ):
                    # A burst (a source restamping a whole window) must not
                    # reach the bound and drop rows a healthy sink would take.
                    # The cache already holds these versions, so one not
                    # queued now is never offered again: a failed checkpoint
                    # is left to the flush below, after every row is queued.
                    # A flush that leaves the queue full is not repeated for
                    # the rest of this fetch.
                    try:
                        self._flush_pending_ohlcv()
                    except Exception:
                        logger.warning(
                            "Checkpoint after flushing a burst of corrections failed",
                            exc_info=True,
                        )
                    sink_took_burst = len(self._pending_ohlcv) < MAX_PENDING_OHLCV
                self._queue_ohlcv({symbol: audit_bar}, event_ts)
                corrections_queued = True
                if not self._durable_ohlcv_delivery:
                    self._deliver_ohlcv({symbol: audit_bar}, event_ts)
            reread.extend(
                (symbol, event_ts, audit_bar)
                for event_ts, audit_bar in by_version(result.audit_rows)
            )
        # Bars already decided before a restart: a refusal is an audit gap,
        # not a reason to skip this cycle's decision.
        self._offer_audit_rows(reread, "restart re-read")
        if corrections_queued and self._durable_ohlcv_delivery:
            # Offered now, as the cycle's first flush was; a refusal keeps
            # them queued for the next checkpoint and retry.
            self._flush_pending_ohlcv()
        return results

    def _record_market_data_fetch_failure(self, symbol: str, error: Exception) -> None:
        failures = self._market_data_fetch_failures.get(symbol, 0) + 1
        self._market_data_fetch_failures[symbol] = failures
        logger.error(
            "Failed to fetch %s (%d consecutive)",
            symbol,
            failures,
            exc_info=(type(error), error, error.__traceback__),
        )
        opens_incident = (
            failures == self.CONSECUTIVE_ERROR_THRESHOLD
            and symbol not in self._market_data_fetch_degraded
        )
        if failures == self.CONSECUTIVE_ERROR_THRESHOLD:
            self._market_data_fetch_degraded.add(symbol)
        if not opens_incident:
            self._record_market_data_fetch_health(symbol, failed=True, error=error)
            return
        if self._on_runtime_event:
            self._on_runtime_event(
                RuntimeEvent(
                    ts=self._utc_now(),
                    event_type="decision_skipped",
                    symbol=symbol,
                    detail={
                        "reason": "market_data_fetch_failed",
                        "consecutive_failures": failures,
                        "error_type": type(error).__name__,
                        "message": str(error),
                    },
                )
            )
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Market Data Fetch Failed: {symbol}",
            message=f"{failures} consecutive failures: {error}",
        )
        self._record_market_data_fetch_health(symbol, failed=True, error=error)

    def _record_market_data_fetch_success(self, symbol: str) -> None:
        failures = self._market_data_fetch_failures.pop(symbol, 0)
        if failures:
            logger.info("Market data fetch recovered for %s after %d failures", symbol, failures)
        self._record_market_data_fetch_health(symbol, failed=False)

    def _record_market_data_fetch_health(
        self,
        symbol: str,
        *,
        failed: bool,
        error: Exception | None = None,
    ) -> None:
        """Maintain a bounded rolling failure diagnostic for one feed."""
        history = self._market_data_fetch_history.setdefault(
            symbol, deque(maxlen=self.FETCH_HEALTH_WINDOW)
        )
        history.append(failed)
        if len(history) < self.FETCH_HEALTH_WINDOW:
            return

        failure_count = sum(history)
        is_degraded = symbol in self._market_data_fetch_degraded
        if not is_degraded and failure_count >= self.FETCH_HEALTH_ALERT_FAILURES:
            self._market_data_fetch_degraded.add(symbol)
            failure_rate = failure_count / self.FETCH_HEALTH_WINDOW
            logger.warning(
                "Market data fetch degraded for %s: failures=%d/%d",
                symbol,
                failure_count,
                self.FETCH_HEALTH_WINDOW,
            )
            if self._on_runtime_event:
                self._on_runtime_event(
                    RuntimeEvent(
                        ts=self._utc_now(),
                        event_type="decision_skipped",
                        symbol=symbol,
                        detail={
                            "reason": "market_data_fetch_degraded",
                            "failed_polls": failure_count,
                            "window_polls": self.FETCH_HEALTH_WINDOW,
                            "failure_rate": failure_rate,
                            "current_poll_failed": failed,
                            "error_type": type(error).__name__ if error else None,
                            "message": str(error) if error else None,
                        },
                    )
                )
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Market Data Fetch Degraded: {symbol}",
                message=(
                    f"{failure_count}/{self.FETCH_HEALTH_WINDOW} recent polls failed "
                    f"({failure_rate:.0%}); failed polls remain cycle-atomic and "
                    "skip strategy evaluation."
                ),
            )
        elif is_degraded and not failed and failure_count <= self.FETCH_HEALTH_RECOVERY_FAILURES:
            self._market_data_fetch_degraded.remove(symbol)
            logger.info(
                "Market data fetch health recovered for %s: failures=%d/%d",
                symbol,
                failure_count,
                self.FETCH_HEALTH_WINDOW,
            )
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Market Data Fetch Recovered: {symbol}",
                message=(
                    f"Recent fetch failures fell to {failure_count}/"
                    f"{self.FETCH_HEALTH_WINDOW}; degradation alert cleared."
                ),
            )

    def _reconcile_positions(self) -> None:
        """Adopt real broker positions into local state at startup.

        Without this, a process restart while a real position is open left
        local positions/cash ledgers assuming flat/full-balance — the local
        book and the broker's actual book could silently diverge forever
        (double-open on the broker, or reject a legitimate opposite-side
        signal while the broker is actually flat).

        No-op in sim mode. Live mode fails closed: if broker positions cannot
        be read, strategy execution is halted rather than starting from an
        assumed-flat local book.
        """
        if self._executor.simulation:
            return
        if self._restored_state:
            try:
                broker_positions = self._read_broker_positions()
            except Exception:
                logger.exception("Broker position reconciliation failed")
                self._halt_live(
                    title="Position Reconciliation Failed",
                    message="Configured broker positions are unavailable",
                )
                return
            if not self._position_books_match(self._positions, broker_positions):
                self._halt_live(
                    title="Position Reconciliation Mismatch",
                    message="Persisted and broker positions differ for configured symbols",
                )
            return
        if self._bootstrap_broker_positions():
            self._persist_state()
            return

        self._halt_live(
            title="Position Reconciliation Failed",
            message="Configured broker positions are unavailable",
        )

    def _read_broker_positions(self) -> dict[str, _BrokerPosition]:
        """Read positions for configured symbols, not the whole account."""
        if self._executor.simulation:
            return {}

        positions: dict[str, _BrokerPosition] = {}
        for symbol in self._symbols:
            broker_pos = self._executor.get_position(symbol)
            if "size" not in broker_pos or broker_pos["size"] is None:
                raise ValueError(f"broker position for {symbol} is missing size")
            try:
                size = float(broker_pos["size"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"broker position for {symbol} has invalid size") from exc
            if not isfinite(size):
                raise ValueError(f"broker position for {symbol} has non-finite size")
            if abs(size) <= EPSILON:
                continue
            # CCXT spot balances carry no cost-basis field, so avg_price is
            # legitimately absent here (unlike size, which every broker
            # returns) — _position_books_match already tolerates None.
            raw_average = broker_pos.get("avg_price")
            avg_price: float | None = None
            if raw_average is not None:
                try:
                    avg_price = float(raw_average)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"open broker position for {symbol} has invalid average price"
                    ) from exc
                if not isfinite(avg_price) or avg_price <= 0:
                    raise ValueError(f"broker returned invalid average price for {symbol}")
            side: Literal["long", "short"] = "long" if size > 0 else "short"
            positions[symbol] = _BrokerPosition(
                side=side,
                quantity=abs(size),
                average_price=avg_price,
            )
        return positions

    def _position_books_match(
        self,
        local: dict[str, PositionState],
        broker: dict[str, _BrokerPosition],
    ) -> bool:
        if set(local) != set(broker):
            return False
        for symbol, expected in local.items():
            actual = broker[symbol]
            tolerance = max(EPSILON, expected.quantity * 1e-9)
            if expected.side != actual.side or abs(expected.quantity - actual.quantity) > tolerance:
                return False
            if actual.average_price is not None:
                price_tolerance = max(
                    self._get_cost_model(symbol).tick_size + EPSILON,
                    abs(expected.entry_price) * 1e-4,
                )
                if abs(expected.entry_price - actual.average_price) > price_tolerance:
                    return False
        return True

    def _reconcile_open_orders(self) -> None:
        """Fail closed when a configured symbol has an untracked open order."""
        if self._executor.simulation:
            return
        known_ids = {order.order_id for order in self._active_orders if order.order_id}
        known_clients: set[str] = set()
        for order in self._active_orders:
            known_clients.add(order.request.client_order_id)
            adapter = self._executor.get_order_adapter(order.request.symbol)
            if adapter is not None:
                # Raw open orders carry the venue's form of the client id.
                known_clients.add(self._executor.broker_client_order_id(adapter, order.request))
        orphans: list[str] = []
        for symbol in self._symbols:
            for raw in self._executor.list_open_orders(symbol):
                order_id = str(raw.get("id") or raw.get("order_id") or "")
                client_id = str(raw.get("clientOrderId") or raw.get("client_order_id") or "")
                if order_id not in known_ids and client_id not in known_clients:
                    orphans.append(f"{symbol}:{order_id or client_id or 'unknown'}")
        if orphans:
            self._halt_live(
                title="Orphan Broker Orders",
                message="Untracked open orders on configured symbols: " + ", ".join(orphans),
            )

    def _bootstrap_broker_positions(self) -> bool:
        """Verify that a first live run starts from a reconstructible book."""
        try:
            snapshot = self._read_broker_positions()
        except Exception:
            logger.exception("Broker position snapshot failed; keeping last confirmed local book")
            return False

        if snapshot:
            symbols = ", ".join(sorted(snapshot))
            self._halt_live(
                title="Non-flat First-run Broker State",
                message=(
                    f"Broker positions exist for configured symbols ({symbols}), but no "
                    "durable checkpoint exists to reconstruct cash, entry costs, and the "
                    "risk epoch. Restore the matching checkpoint or flatten those "
                    "positions before starting this deployment"
                ),
            )
            return True

        self._positions = {}
        return True

    # 1% — same style as CONSECUTIVE_ERROR_THRESHOLD: an engine constant,
    # not a config.params knob (nothing in this run should reasonably need a
    # different tolerance).
    CASH_RECONCILE_TOLERANCE_PCT = 0.01

    def _reconcile_cash(self) -> None:
        """Best-effort: alert on cash/broker drift at startup, never
        auto-adjusts local account cash.

        Unlike _reconcile_positions (where the broker's side/quantity is
        unambiguous and a wrong local position is actively dangerous for
        signal generation), "free"/"total" balance semantics vary by
        account mode and don't map cleanly onto this engine's cash concept
        — auto-overwriting risks replacing a good local number with a
        misread one. Detect and alert, let a human decide.

        Duck-typed and best-effort: an unavailable capability or unreadable
        balance is reported but does not halt trading because broker balance
        semantics cannot safely replace the local accounting ledger. A
        BrokerUnavailableError propagates so the caller can count the round.
        """
        if self._executor.simulation:
            return

        adapter = self._executor.get_order_adapter(self._symbols[0])
        if not callable(getattr(adapter, "get_balance", None)):
            logger.warning(
                "Cash reconciliation unavailable for account=%s: "
                "order adapter has no get_balance capability",
                self._account_id,
            )
            return
        try:
            broker_total = float(adapter.get_balance(self._currency)["total"])
        except BrokerUnavailableError:
            raise
        except Exception:
            logger.exception(
                "Cash reconciliation failed for account=%s currency=%s; skipping",
                self._account_id,
                self._currency,
            )
            return
        if not isfinite(broker_total):
            logger.warning(
                "Cash reconciliation returned non-finite total for account=%s; skipping",
                self._account_id,
            )
            return
        drift_pct = abs(broker_total - self._cash) / max(self._cash, EPSILON)
        if drift_pct <= self.CASH_RECONCILE_TOLERANCE_PCT:
            return
        logger.warning(
            "Cash drift: account=%s local=%.2f broker=%.2f (%s), not auto-adjusted",
            self._account_id,
            self._cash,
            broker_total,
            self._currency,
        )
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Cash Reconciliation Drift",
            message=(
                f"account_id={self._account_id} local_cash={self._cash:.2f} "
                f"broker_balance={broker_total:.2f} ({self._currency}) "
                f"drift={drift_pct:.2%}, review manually"
            ),
        )

    def _maybe_reconcile_runtime(self) -> None:
        """Periodically compare broker facts without mutating the local ledger."""
        if (
            self._executor.simulation
            or self._halted
            or self._active_orders
            or self._live_rebalance is not None
        ):
            return
        now = self._utc_now()
        if (
            self._last_reconciliation_at is not None
            and (now - self._last_reconciliation_at).total_seconds()
            < self._reconciliation_interval_seconds
        ):
            return
        # Stamped before the reads so an unavailable round waits a full
        # interval too: rounds map to unverified time and a struggling venue
        # is not polled harder.
        self._last_reconciliation_at = now
        try:
            broker_positions = self._read_broker_positions()
            if self._position_books_match(self._positions, broker_positions):
                self._reconcile_open_orders()
                if not self._halted:
                    self._reconcile_cash()
            else:
                self._halt_live(
                    title="Periodic Position Reconciliation Mismatch",
                    message="Local and broker positions differ for configured symbols",
                )
        except BrokerUnavailableError as exc:
            self._record_reconciliation_unavailable(exc)
            return
        except Exception as exc:
            if self._halted:
                # A halt in this round has alerted already; its error is not
                # a reconciliation failure.
                raise
            logger.exception("Periodic broker reconciliation failed")
            self._halt_live(
                title="Periodic Reconciliation Failed",
                message=str(exc),
            )
        if not self._halted:
            self._resume_unverified = False
            self._reconciliation_unavailable_rounds = 0
            self._record_reconciliation_health(skipped=False)

    def _record_reconciliation_unavailable(self, error: BrokerUnavailableError) -> None:
        rounds = self._reconciliation_unavailable_rounds + 1
        self._reconciliation_unavailable_rounds = rounds
        logger.warning(
            "Periodic broker reconciliation skipped: broker unavailable (%d consecutive)",
            rounds,
            exc_info=error,
        )
        if rounds >= self.RECONCILIATION_UNAVAILABLE_HALT_ROUNDS:
            self._halt_live(
                title="Periodic Reconciliation Unavailable",
                message=f"Broker unavailable for {rounds} consecutive rounds: {error}",
            )
            return
        self._record_reconciliation_health(skipped=True, error=error)

    def _record_reconciliation_health(
        self, *, skipped: bool, error: BrokerUnavailableError | None = None
    ) -> None:
        """Alert on skipped rounds over a window, as market-data fetch health does.

        Unlike that feed diagnostic, a partial window counts, so a pure outage
        alerts before the consecutive halt bound.
        """
        history = self._reconciliation_history
        history.append(skipped)
        skipped_rounds = sum(history)
        if not self._reconciliation_degraded and skipped_rounds >= self.FETCH_HEALTH_ALERT_FAILURES:
            self._reconciliation_degraded = True
            trading = (
                "new decisions wait for an answered round"
                if self._resume_unverified
                else "trading continues"
            )
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Periodic Reconciliation Skipped",
                message=(
                    f"{skipped_rounds}/{len(history)} recent rounds got no broker answer "
                    f"({error}); {trading}, halts after "
                    f"{self.RECONCILIATION_UNAVAILABLE_HALT_ROUNDS} consecutive."
                ),
            )
        elif (
            self._reconciliation_degraded
            and not skipped
            and skipped_rounds <= self.FETCH_HEALTH_RECOVERY_FAILURES
        ):
            self._reconciliation_degraded = False
            logger.info(
                "Periodic broker reconciliation recovered: skipped=%d/%d",
                skipped_rounds,
                len(history),
            )
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Periodic Reconciliation Recovered",
                message=f"Skipped rounds fell to {skipped_rounds}/{len(history)}; alert cleared.",
            )

    def _halt_live(self, *, title: str, message: str) -> None:
        """Fail closed and cancel every tracked order that may still execute.

        The halt is checkpointed before cancelling, and each cancel records
        itself. A failure of either still alerts, naming it, before it
        propagates.
        """
        self._enter_halt()
        failure, note = self._persist_halt(cancel_on_retry=True)
        if failure is None and not self._executor.simulation:
            failure, note = self._cancel_for_halt()
        logger.error("%s: %s", title, message)
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] {title}",
            message=f"{message}; trading halted{note}.",
        )
        self._record_runtime_event("halted", {"reason": title, "message": message})
        if failure is not None:
            raise failure

    def _record_runtime_event(
        self,
        event_type: RuntimeEventType,
        detail: Mapping[str, object],
        *,
        symbol: str | None = None,
    ) -> None:
        """Offer a halt, reset or group failure once the action it records is done.

        Coming last, a slow sink cannot delay the action; a failing one is
        logged rather than raised, so it neither undoes the action nor hides
        the action's own error. Stamped with the wall clock, not a bar ts:
        several can fall in one bar, and the checkpoint or broker call each
        makes first keeps two from sharing a timestamp, which the table's
        dedupe key would merge.
        """
        if not self._on_runtime_event:
            return
        try:
            self._on_runtime_event(
                RuntimeEvent(
                    ts=self._utc_now(), event_type=event_type, symbol=symbol, detail=detail
                )
            )
        except Exception:
            logger.exception("Runtime event %s not recorded", event_type)

    def _enter_halt(self) -> None:
        """Mark the account halted, dropping work and any reset that must not outlive it."""
        self._halted = True
        self._pending_decision = []
        self._pending_resting_since = {}
        self._live_rebalance = None
        self._entry_fill_unchecked = None
        self._drop_pending_halt_reset()

    def _persist_halt(self, *, cancel_on_retry: bool) -> tuple[Exception | None, str]:
        """Checkpoint a halt, or keep it for retry and return a note naming the failure.

        ``cancel_on_retry`` is whether the halt still owes a cancellation: a
        plain halt does, a flatten leaves its recovery exits working.
        """
        try:
            self._persist_state()
        except Exception as exc:
            self._halt_unpersisted = True
            self._halt_cancels_pending = cancel_on_retry and not self._executor.simulation
            # The caller re-raises it, which logs the traceback.
            logger.error("Halt checkpoint not written: %s", exc)
            return exc, (
                f"; halt not persisted ({type(exc).__name__}: {exc}), "
                "so a restart now would resume unhalted"
            )
        return None, ""

    def _cancel_for_halt(self) -> tuple[Exception | None, str]:
        """Cancel every tracked order, keeping the cancellation owed until the venue confirms.

        Returns the pass's first failure, if any, and a note naming it and
        every order still tracked: a cancel the venue has not confirmed may
        still fill.
        """
        failure: Exception | None = None
        note = ""
        try:
            self._cancel_active_orders()
        except Exception as exc:
            failure = exc
            note = f"; cancelling tracked orders failed ({type(exc).__name__}: {exc})"
        self._halt_cancels_pending = failure is not None or bool(self._active_orders)
        if self._active_orders:
            note += f"; awaiting cancel confirmation for {self._tracked_orders_summary()}"
        return failure, note

    def _tracked_orders_summary(self) -> str:
        """Each tracked order with the status the venue last reported for it."""
        return ", ".join(
            f"{tracked.request.symbol} order_id={tracked.order_id or 'unassigned'} "
            f"({tracked.status})"
            for tracked in self._active_orders
        )

    def _send_alert(self, title: str, message: str) -> None:
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] {title}",
            message=message,
        )

    def _alert_report_not_applied(
        self, tracked: TrackedOrder, message: str, *, content: str | None = None
    ) -> None:
        self._report_alerts.fire(
            tracked.request.client_order_id,
            "Order Report Not Applied",
            message,
            content=content,
        )

    def _alert_unapplied(self, exc: ReportNotAppliedError, note: str = "") -> None:
        self._alert_report_not_applied(exc.order, f"{exc}{note}", content=f"{exc.content}{note}")

    def _alert_unapplied_while_halted(self, exc: ReportNotAppliedError) -> None:
        """Alert, without failing the cycle: a halted account does no order work to protect."""
        owed = "; the halt's cancellation stays owed" if exc.order in self._active_orders else ""
        self._alert_unapplied(exc, owed)

    def _retry_halt(self) -> bool:
        """Finish a halt's checkpoint, then its owed cancellation, before any other work.

        A failure of either propagates, so no order work runs on a state the
        store never saw or ahead of the cancellation, and the next cycle tries
        again. Returns whether it sent the cancellation pass, which already
        polled and cancelled every tracked order.
        """
        if self._halt_unpersisted:
            self._persist_state()
            self._halt_unpersisted = False
            logger.warning("Halt checkpoint persisted")
            failure, note = None, ""
            cancelled = self._halt_cancels_pending
            if cancelled:
                failure, note = self._cancel_for_halt()
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Halt Persisted",
                message=f"The halt is now recorded; a restart resumes halted{note}.",
            )
            if failure is not None:
                raise failure
            return cancelled
        if self._halt_cancels_pending:
            # Owed while any order is tracked, since a cancel the venue has not
            # confirmed may still fill; the halt alert named what was left.
            self._cancel_active_orders()
            if self._active_orders:
                return True
            self._halt_cancels_pending = False
            logger.warning("Halt cancellation completed")
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Halt Cancellation Completed",
                message="The venue confirmed every cancel the halt owed; no order is tracked.",
            )
            return True
        return False

    def _fail_group_or_halt(
        self,
        tracked: TrackedOrder,
        *,
        title: str,
        message: str,
        account_fault: bool = False,
    ) -> bool:
        """Scope one leg's *confirmed* terminal failure to its group when possible.

        Only call this once the broker has given a definite answer for
        `tracked` (report.status in ("cancelled", "rejected"), already
        reflected via _apply_order_report — tracked is no longer in
        _active_orders by the time this runs). Anything short of that —
        ambiguous placement, an unresolved timeout, a cancellation attempt
        that raised — means the broker-side state of `tracked` itself is
        unknown, not just its outcome, and must still fail the whole account
        closed via _halt_live regardless of group_id: scoping an unknown
        state to "cancel the group and keep going" risks silently losing
        track of an order that may still be live at the venue.

        An exit of a flatten that has halted, or is halting, the account is
        recorded as a skipped close, even for an account fault: every other
        exit may still be working, and the account stops trading anyway.
        Otherwise a leg with no group_id halts, having no isolation boundary,
        and so does an account fault, which every later order would repeat.
        A grouped leg's confirmed failure cancels
        only that group's other still-active legs and alerts — unrelated
        groups and independent intents keep executing. A target rebalance
        stops instead, since replanning it would send the same leg again;
        the next bar's decision replans from the book. Returns True if the
        whole account was halted (caller should stop advancing this tick),
        False otherwise (caller should continue to the next active order).
        """
        request = tracked.request
        if (self._halted or self._flatten_at is not None) and _is_recovery_exit(request):
            position = self._positions.get(request.symbol)
            self._report_skipped_close(
                request.symbol,
                quantity=request.quantity - tracked.filled_quantity,
                held=position.quantity if position is not None else None,
                ts=self._flatten_at or self._utc_now(),
                reason=f"close_{tracked.status}",
                title=f"Close {tracked.status.title()}",
                cause=f"{title}: {message}",
            )
            return False
        group_id = request.group_id
        if group_id is None or account_fault:
            self._halt_live(title=title, message=message)
            return True
        if not self._executor.simulation:
            for sibling in list(self._active_orders):
                if sibling.request.group_id == group_id:
                    self._cancel_tracked_order(sibling)
        rebalance_stopped = self._live_rebalance is not None
        if rebalance_stopped:
            self._live_rebalance = None
            self._persist_state()
        scope = "the target rebalance stopped" if rebalance_stopped else "other groups unaffected"
        outcome = f"{message}; group {group_id!r} cancelled, {scope}"
        logger.error("%s: %s", title, outcome)
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] {title}",
            message=f"{outcome}.",
        )
        self._record_runtime_event(
            "group_failed",
            {
                "reason": title,
                "group_id": group_id,
                "client_order_id": request.client_order_id,
                "message": message,
                "rebalance_stopped": rebalance_stopped,
            },
            symbol=request.symbol,
        )
        return False

    def _post_fill_risk_halted(self) -> bool:
        """Halt if an entry fill booked since the last check took the account past a limit.

        Any report can carry a fill, whichever path applied it: a status
        read, or the answer to a timeout's or another engine cancel. Not
        while the account is halted, or a flatten submits its exits: both are
        closing the book already, and a halt here would cancel the exits.
        Returns whether it halted.
        """
        filled, self._entry_fill_unchecked = self._entry_fill_unchecked, None
        if filled is None or self._halted or self._flatten_at is not None:
            return False
        violation = self._post_fill_risk_violation()
        if violation is None:
            return False
        self._halt_live(
            title="Post-fill Risk Breach",
            message=(
                f"{violation}; after {filled.request.symbol} order_id={filled.order_id} "
                f"status={filled.status} "
                f"filled={filled.filled_quantity:.4f}/{filled.request.quantity:.4f}"
            ),
        )
        return True

    def _has_active_recovery_orders(self) -> bool:
        """Whether every tracked order is an engine-owned recovery order."""
        return bool(self._active_orders) and all(
            _is_recovery_exit(tracked.request) for tracked in self._active_orders
        )

    def _apply_manual_halt(self, reason: str) -> bool:
        """Halt for an operator, leaving an already-halted account's exits working.

        A halted account's only live orders are recovery exits closing its
        positions; cancelling them would leave the positions open. Returns
        whether it sent a halt's owed cancellation pass, as ``_retry_halt``.
        """
        if not self._halted:
            self._halt_live(title="Manual Halt", message=reason)
            return False
        if self._halt_unpersisted or self._halt_cancels_pending:
            # Answer with the finished halt, its error, or what it still owes.
            logger.warning("Manual Halt: %s; already halted, not yet finished", reason)
            try:
                cancelled = self._retry_halt()
            except ReportNotAppliedError as exc:
                # A signalled halt gets no other answer: the cycle goes on.
                self._notify(
                    "send_alert",
                    title=f"[{self._executor.strategy_name}] Manual Halt",
                    message=f"{reason}; already halted; cancellation owed: {exc}",
                )
                raise
            if self._halt_cancels_pending:
                message = (
                    f"{reason}; already halted; cancellation owed, "
                    f"{len(self._active_orders)} tracked"
                )
                if self._active_orders:
                    message += f": {self._tracked_orders_summary()}"
                logger.warning("Manual Halt: %s", message)
                self._notify(
                    "send_alert",
                    title=f"[{self._executor.strategy_name}] Manual Halt",
                    message=message,
                )
            return cancelled
        exits = sum(1 for tracked in self._active_orders if _is_recovery_exit(tracked.request))
        message = f"{reason}; already halted; {exits} recovery exits keep working"
        logger.warning("Manual Halt: %s", message)
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Manual Halt",
            message=message,
        )
        return False

    def _initialize_run(self) -> None:
        """Acquire live ownership and reconcile broker facts before polling."""
        if not self._executor.simulation:
            if not self._state_store.acquire_lease(self._account_lease_key):
                raise RuntimeError(
                    "another live process already owns execution account "
                    f"({self._execution_identity.summary}); configured "
                    f"account_id={self._account_id!r}"
                )
            self._account_lease_acquired = True
            if not self._state_store.acquire_lease(self._state_key):
                raise RuntimeError(
                    f"another live process already owns state_key={self._state_key!r}"
                )
            self._lease_acquired = True
            # Not persisted: a start decides from the checkpoint, as a new process does.
            self._halt_cancels_pending = False
            restored_halted = self._halted
            restored_orders = bool(self._active_orders)
            try:
                if self._halted and not self._has_active_recovery_orders():
                    self._cancel_active_orders()
                    self._halt_cancels_pending = bool(self._active_orders)
                    # The halt that owed these cancels alerted in the process
                    # that is gone, so this one says how its own pass ended.
                    if self._halt_cancels_pending:
                        self._send_alert(
                            "Halt Cancellation Owed",
                            "restarted halted; awaiting cancel confirmation for "
                            f"{self._tracked_orders_summary()}",
                        )
                    elif restored_orders:
                        self._send_alert(
                            "Halt Cancellation Completed",
                            "restarted halted; the venue confirmed every cancel the halt "
                            "owed; no order is tracked.",
                        )
                else:
                    self._advance_live_orders(submit_planned=False)
                self._reconcile_open_orders()
            except Exception as exc:
                if self._halted and not restored_halted:
                    # A halt during reconciliation has alerted already.
                    raise
                logger.exception("Broker order reconciliation failed")
                self._halt_live(
                    title="Order Reconciliation Failed",
                    message=str(exc),
                )

        self._reconcile_positions()
        try:
            self._reconcile_cash()
        except BrokerUnavailableError:
            # Startup cash is best-effort; positions and orders gate the start.
            logger.warning("Startup cash reconciliation skipped: broker unavailable", exc_info=True)
        # Startup reconciliation is fail-closed, so it verifies a reset too;
        # simulation has nothing to verify.
        self._resume_unverified = False
        if not self._executor.simulation:
            self._last_reconciliation_at = self._utc_now()

    def _release_lease(self) -> None:
        if self._lease_acquired:
            self._state_store.release_lease(self._state_key)
            self._lease_acquired = False
        if self._account_lease_acquired:
            self._state_store.release_lease(self._account_lease_key)
            self._account_lease_acquired = False

    def run(self, max_iterations: int | None = None) -> None:
        """Start the polling loop. Blocks until stopped or max_iterations reached."""
        self._drain_wake()
        # Made while no run owned the account, so not after a review of this one.
        if self._drain_halt_reset_requests():
            logger.warning("Dropped a halt reset request made before run()")
        self._running = True
        self._closed = False
        self._setup_signal_handlers()
        try:
            with self._cycle_lock:
                self._initialize_run()
            if self._on_ready:
                self._on_ready(self._run_id)
        except BaseException:
            self._running = False
            with self._cycle_lock:
                self._close()
            raise
        iteration = 0
        strategy_name = self._executor.strategy_name
        symbols_str = ",".join(self._symbols)

        logger.info(
            "LiveTrader started: symbols=%s, timeframe=%s, poll=%ss",
            self._symbols,
            self._timeframe,
            self._poll_seconds,
        )
        self._notify(
            "send_startup",
            strategy=strategy_name,
            symbol=symbols_str,
            mode=self._config.mode,
            run_id=self._run_id,
        )

        shutdown_reason = "normal"
        try:
            while self._running:
                with self._cycle_lock:
                    cycle_started_at = self._utc_now()
                    cycle_started = perf_counter()
                    self._cycle_fetch_seconds = {}
                    self._cycle_strategy_seconds = 0.0
                    self._cycle_order_seconds = 0.0
                    try:
                        self._poll_cycle()
                        self._consecutive_errors = 0
                    except Exception as exc:
                        self._consecutive_errors += 1
                        logger.exception(
                            "Error in poll cycle (%d consecutive), will retry next interval",
                            self._consecutive_errors,
                        )
                        if self._consecutive_errors == self.CONSECUTIVE_ERROR_THRESHOLD:
                            self._notify(
                                "send_alert",
                                title=f"[{strategy_name}] Poll Error",
                                message=(
                                    f"{self._consecutive_errors} consecutive failures, the "
                                    f"last {type(exc).__name__}: {exc}. Check logs."
                                ),
                            )
                    finally:
                        self._finish_cycle_diagnostics(cycle_started_at, cycle_started)

                iteration += 1
                if max_iterations is not None and iteration >= max_iterations:
                    logger.info("Reached max_iterations=%d, stopping", max_iterations)
                    break

                if self._running:
                    diagnostics = self._last_cycle_diagnostics
                    cycle_seconds = diagnostics.cycle_seconds if diagnostics else 0.0
                    self._sleep(max(0.0, self._poll_seconds - cycle_seconds))
        except Exception:
            shutdown_reason = "unhandled exception"
            logger.exception("LiveTrader crashed")
        finally:
            # Under the lock, so an operator call waiting on the last cycle
            # finds the run closed instead of acting after the lease is gone.
            with self._cycle_lock:
                try:
                    self._notify(
                        "send_shutdown",
                        strategy=strategy_name,
                        symbol=symbols_str,
                        reason=shutdown_reason,
                    )
                    self._notify_pool.shutdown(wait=True)
                finally:
                    self._close()
            logger.info("LiveTrader stopped (reason: %s)", shutdown_reason)

    def _close(self) -> None:
        """Refuse further operator calls and release the account; hold the cycle lock."""
        self._closed = True
        dropped = self._drain_halt_requests()
        if dropped:
            logger.warning("Dropped halt requests at shutdown: %s", "; ".join(dropped))
        if self._drain_halt_reset_requests():
            logger.warning("Dropped a halt reset request at shutdown")
        self._release_lease()

    def _require_running(self) -> None:
        if self._closed:
            raise RuntimeError("trader is not running: run() has shut down")
        # Without the leases another process may own the account: acting here
        # would write its checkpoint, cancel its orders and alert, while that
        # process trades on unchanged.
        if not self._executor.simulation and not (
            self._lease_acquired and self._account_lease_acquired
        ):
            raise RuntimeError("trader is not running: run() has not taken the account leases")

    def stop(self) -> None:
        """Signal the runner to stop after the current cycle."""
        self._running = False
        self._wake.put(None)

    def _wait_for_next_cycle(self, seconds: float) -> None:
        """Sleep until the next cycle is due or a stop or halt request wakes it."""
        try:
            self._wake.get(timeout=seconds)
        except Empty:
            return
        # The next cycle answers every request made so far.
        self._drain_wake()

    def _drain_wake(self) -> None:
        while True:
            try:
                self._wake.get_nowait()
            except Empty:
                return

    @property
    def last_cycle_diagnostics(self) -> CycleDiagnostics | None:
        """Return measured latency for the latest poll cycle."""
        return self._last_cycle_diagnostics

    @property
    def warmup_ready(self) -> bool:
        """Whether every required symbol has enough usable feature history.

        An optional subscription cannot hold the run at the gate: the strategy
        declared it can proceed without that input.
        """
        return not self._replay_backlog_exhausted and all(
            self._warmup_gap(symbol, self._ohlcv_cache.get(symbol)) is None
            for symbol in self._symbols
            if symbol not in self._optional_symbols
        )

    @property
    def ohlcv_audit_degraded(self) -> bool:
        """Whether audit rows are being dropped because delivery cannot catch up.

        Terminal for the audit trail, not for trading: the dropped identities
        are recorded individually and the bars are re-fetchable.
        """
        return self._ohlcv_audit_degraded

    @property
    def position_event_delivery_degraded(self) -> bool:
        """Whether position event delivery is failing, set at the first refusal.

        Clears after ``POSITION_EVENT_RECOVERY_CYCLES`` clean poll cycles. What
        was dropped meanwhile is in ``dropped_position_event_sequences``.
        """
        return self._position_event_delivery_degraded

    @property
    def dropped_position_event_sequences(self) -> tuple[int, ...]:
        """Every position event sequence this process dropped undelivered, oldest first.

        The backfill list when the runtime-event sink shares the store that
        refused the events and could not record the drops either.
        """
        return tuple(self._dropped_position_event_sequences)

    @property
    def order_ledger_degraded(self) -> bool:
        """Whether the state store refused an order row, which was dropped.

        Stays set for the process: the ledger has a gap until the row named in
        its runtime event is restored.
        """
        return self._order_ledger_degraded

    @property
    def run_id(self) -> str:
        """Stable id used by callbacks and persisted runtime facts."""
        return self._run_id

    def halt(self, reason: str = "operator requested halt") -> None:
        """Fail closed until an operator calls ``reset_halt``.

        Safe from any thread. The halt is recorded before waiting for the
        cycle in progress, then applied once that cycle ends, raising any
        error to the caller. A cycle that starts first, or never ends,
        applies it itself at its start, so a slow or hung cycle cannot lose it.
        Raises unless ``run`` holds the account: in live mode until it has
        taken its leases (a call made while it takes them waits), and once it
        has shut down. Refused before the leases, the halt stays queued, and
        the first cycle of ``run`` applies it, as it does ``request_halt``.
        """
        reason = _validated_reason(reason, "halt")
        if not self._closed:
            self._halt_requests.put(reason)
        with self._cycle_lock:
            try:
                self._require_running()
            except RuntimeError as exc:
                if self._closed:
                    # Shut down while this call waited: nothing may apply it now.
                    self._drain_halt_requests()
                    raise
                # Only this process's loop applies the queue, once it holds the
                # leases; dropping the halt could lose one made as run() starts.
                raise RuntimeError(
                    f"{exc}; the halt stays queued for the first cycle of run()"
                ) from None
            self._order_pass += 1
            self._run_requested_halt()

    def request_halt(self, reason: str = "operator requested halt") -> None:
        """Ask the polling loop to halt as ``halt`` does, without waiting.

        Safe from any thread and from a signal handler: it only records the
        request and wakes a sleeping loop. The loop applies it first thing in
        its next cycle, before any order work or a pending flatten; a cycle
        already running finishes first. A restart drops a pending one.
        """
        self._halt_requests.put(_validated_reason(reason, "halt"))
        self._wake.put(None)

    def request_halt_reset(self) -> None:
        """Ask the polling loop to ``reset_halt``, without waiting.

        Safe from any thread and from a signal handler: it only records the
        request and wakes a sleeping loop. The loop applies it early in its
        next cycle, before any order work, and alerts the outcome, since no
        caller waits for an error. Any halt applied while it is pending drops
        it, and a halt requested as it applies lands right after it, so a
        reset never undoes a later halt. An account that is not halted is left
        alone. A restart drops a pending one.
        """
        self._halt_reset_requests.put(None)
        self._wake.put(None)

    def request_flatten(self, reason: str = "operator requested flatten") -> None:
        """Ask the polling loop to close every position and halt, as a drawdown breach does.

        Safe from any thread: it only records the request. The loop acts on it
        at the start of its next cycle, before strategy evaluation, after
        cancelling any working order. A halted account refuses it, and the
        request is not persisted, so a restart drops a pending one.
        """
        reason = _validated_reason(reason, "flatten")
        with self._flatten_request_lock:
            self._flatten_request_sequence += 1
            self._flatten_request = (self._flatten_request_sequence, reason)

    def halt_reset_readiness(self) -> HaltResetReadiness:
        """Whether ``reset_halt`` would be allowed, and why not.

        A query, never a mutation: health tooling and an operator should be
        able to see the blocking reason without provoking an exception to
        obtain it. Remaining halted is always safe; being unable to find out
        why is not. Safe from any thread; it waits for the cycle in progress,
        and raises unless ``run`` holds the account, as ``halt`` does, rather
        than answer for a checkpoint another process may be changing.

        Every open position needs a mark that is both present and current.
        Presence alone would let a reset revalue the book on a price a dead
        feed last produced days ago; freshness reuses the same
        per-subscription evaluator the runtime uses for market data, so a
        closed market is not mistaken for a stalled one.
        """
        with self._cycle_lock:
            self._require_running()
            return halt_recovery.evaluate_reset_readiness(
                halt_persisted=not self._halt_unpersisted,
                active_orders=self._active_orders,
                positions=self._positions,
                last_prices=self._last_prices,
                last_bar_ts=self._last_bar_ts,
                subscriptions=self._market_data_subscriptions,
                now=self._utc_now(),
                staleness_grace=self._staleness_grace,
            )

    def reset_halt(self) -> None:
        """Start a new risk epoch after operator review.

        Fails closed with the structured reason from ``halt_reset_readiness``
        rather than an unhandled valuation error, and records the refusal so
        an operator can see it after the fact. Safe from any thread, waiting
        for the cycle in progress as ``halt`` does; a halt request still
        pending then applies on the next cycle. Raises unless ``run`` holds
        the account, as ``halt`` does.
        """
        with self._cycle_lock:
            readiness = self.halt_reset_readiness()
            if not readiness.ready:
                halt_recovery.refuse_reset(
                    readiness,
                    on_runtime_event=self._on_runtime_event,
                    utc_now=self._utc_now,
                )
            equity, _ = self._calc_account_snapshot()
            was_halted = self._halted
            if was_halted:
                # The operator may have touched the account while halted:
                # compare it with the broker on the next cycle, before any new
                # decision.
                self._last_reconciliation_at = None
                self._resume_unverified = True
            self._halted = False
            # A reset needs every tracked order resolved: nothing is left to cancel.
            self._halt_cancels_pending = False
            # A new risk epoch starts a fresh unverified window.
            self._reconciliation_unavailable_rounds = 0
            self._reconciliation_history.clear()
            self._reconciliation_degraded = False
            self._skipped_close_alerts.clear()
            self._unfunded_entry_alerts.clear()
            self._equity_peak = equity
            self._prev_equity = equity
            self._status_window_equity = equity
            detail = {"was_halted": was_halted, "equity": equity}
            try:
                self._persist_state()
            except Exception:
                # The reset already applied in memory, so it is recorded too.
                self._record_runtime_event("halt_reset", {**detail, "persisted": False})
                raise
            self._record_runtime_event("halt_reset", {**detail, "persisted": True})

    def pause_entries(self, reason: str = "operator paused entries") -> None:
        """Stop submitting orders that open or add to a position, until ``resume_entries``.

        Live mode only, since it gates broker submission. It takes the cycle
        lock, as ``halt`` does, so it never lands mid-submission, and raises
        unless ``run`` holds the account, as ``halt`` does. Every successful
        call alerts, so a pause whose first write failed is still announced
        once a repeat lands. Behavior: the runbook's Pausing entries.
        """
        reason = _validated_reason(reason, "pause")
        if self._executor.simulation:
            raise RuntimeError("pause_entries gates broker order submission; it needs live mode")
        with self._cycle_lock:
            self._require_running()
            self._entries_paused = True
            self._persist_state()
            logger.warning("Entries paused: %s", reason)
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Entries Paused",
                message=f"{reason}; orders that open or add are dropped, exits keep working.",
            )

    def resume_entries(self) -> None:
        """Let orders that open or add go out again after ``pause_entries``.

        Raises unless ``run`` holds the account, as ``halt`` does.
        """
        with self._cycle_lock:
            self._require_running()
            if not self._entries_paused:
                return
            self._entries_paused = False
            try:
                self._persist_state()
            except Exception:
                # Fail closed in memory, whatever the store kept.
                self._entries_paused = True
                raise
            logger.warning("Entries resumed")
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Entries Resumed",
                message="Orders that open or add go out again.",
            )

    def _setup_signal_handlers(self) -> None:
        """Map SIGTERM/SIGINT to shutdown, SIGUSR1 to a halt request, SIGUSR2 to a reset request."""

        def _handler(signum: int, frame: types.FrameType | None) -> None:
            logger.info("Received signal %d, shutting down gracefully", signum)
            self.stop()

        # Both run on the loop thread, possibly mid-cycle: record, never apply.
        def _halt_handler(signum: int, frame: types.FrameType | None) -> None:
            self.request_halt("SIGUSR1 operator halt")

        def _halt_reset_handler(signum: int, frame: types.FrameType | None) -> None:
            self.request_halt_reset()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
        if hasattr(signal, "SIGUSR1"):  # POSIX only, like SIGUSR2
            signal.signal(signal.SIGUSR1, _halt_handler)
            signal.signal(signal.SIGUSR2, _halt_reset_handler)

    def _poll_cycle(self) -> None:
        """Process completed market-data events without live catch-up orders."""
        self._order_pass += 1
        # An order that ended once its answers applied again resolves; one
        # that ended in the pass an answer failed was alerted as such.
        tracked_ids = {tracked.request.client_order_id for tracked in self._active_orders}
        self._report_alerts.retain(tracked_ids)
        self._report_failed_pass = {
            client_order_id: failed_in
            for client_order_id, failed_in in self._report_failed_pass.items()
            if client_order_id in tracked_ids
        }
        # A halt's cancel pass that meets an answer it cannot apply alerts
        # instead of failing the cycle, so reconciliation still runs. A
        # requested halt has already alerted it, naming the order.
        try:
            cancelled = self._run_requested_halt()
        except ReportNotAppliedError:
            logger.exception("Requested halt left its cancellation owed")
            cancelled = True
        try:
            # An owed halt cancellation has already polled and cancelled every
            # tracked order once this cycle; advancing would repeat the head's.
            cancelled = cancelled or self._retry_halt()
        except ReportNotAppliedError as exc:
            self._alert_unapplied_while_halted(exc)
            cancelled = True
        finally:
            # After the retry, so a failing reset cannot skip a halt's owed
            # work; even after a failed one, so a reset is answered in the
            # first cycle after it arrives and never held: refused while the
            # halt's checkpoint is still unwritten, applied once this retry
            # has written it.
            if self._run_requested_halt_reset():
                # A halt requested while the reset applied still wins this cycle.
                cancelled = self._run_requested_halt() or cancelled
        # Order work that hit a store outage may have left ledger rows and
        # fills behind, with no later order work to carry them.
        if self._unpersisted_orders:
            self._persist_state()
        self._publish_checkpointed_results()
        self._flush_pending_position_events(count_attempt=True)
        if not self._executor.simulation and self._active_orders and not cancelled:
            self._advance_live_orders()
        self._run_requested_flatten()
        # Retry unacknowledged audit rows every cycle, not only when a new bar
        # arrives: a stalled feed would otherwise leave them queued
        # indefinitely while the database recovers.
        self._flush_pending_ohlcv()
        self._maybe_reconcile_runtime()
        fetch_results = self._fetch_runtime_frames()
        if any(result.outcome != "success" for result in fetch_results.values()):
            if not self.warmup_ready:
                self._report_incomplete_warmup()
            return
        if self._on_heartbeat:
            self._on_heartbeat(self._run_id)
        if not self.warmup_ready:
            self._report_incomplete_warmup()
            return
        if self._reported_warmup_reasons:
            logger.info(
                "Live warmup ready: required=%d usable=%s",
                self._warmup_periods,
                {symbol: len(self._ohlcv_cache.get(symbol, ())) for symbol in self._symbols},
            )
            self._reported_warmup_reasons.clear()

        if self._auxiliary_subscriptions:
            self._refresh_auxiliary_cache()

        frames: dict[str, pd.DataFrame] = {}
        for symbol, result in fetch_results.items():
            df = result.frame
            if df is None or df.empty:
                continue

            latest = pd.Timestamp(df["ts"].iloc[-1])
            if latest.tzinfo is None:
                raise ValueError(f"{symbol} latest completed bar timestamp must be timezone-aware")
            latest_ts = latest.to_pydatetime().astimezone(UTC)
            # Simulation runs against a live feed too, so an observation past
            # its expected close means the same thing there as in live: this
            # cycle has no usable market event for that subscription. Backtest
            # replays history and never reaches here, where wall-clock
            # staleness would be meaningless.
            if self._check_staleness(symbol, latest_ts):
                logger.warning("Skipping stale frame for %s at %s", symbol, latest_ts)
                continue
            # warmup_ready does not wait on an optional symbol, so an optional
            # one can still be short of history here. Omitting it is what
            # "stepped over" has to mean: a strategy handed one bar would
            # compute indicators over one bar.
            if self._warmup_gap(symbol, self._ohlcv_cache.get(symbol)) is not None:
                logger.info("Omitting under-warmed optional market data: %s", symbol)
                continue
            frames[symbol] = df

        # Report before the active-order gate below, so an outage that starts
        # or clears while an order rests is still announced once; only the hold
        # itself waits for that gate.
        data_not_ready = self._report_data_readiness(sorted(set(self._symbols) - set(frames)))

        # Keep heartbeat, cache, and staleness monitoring alive while a broker
        # order is resting. Strategy evaluation remains serialized behind the
        # active order so a later bar cannot create a conflicting order queue,
        # and after a reset behind a periodic round that answered and matched.
        if not self._executor.simulation and (
            self._active_orders or self._halted or self._resume_unverified
        ):
            return

        # A required subscription with no usable observation blocks evaluation
        # rather than letting the strategy silently decide on a partial view.
        # Reconciliation, order monitoring and heartbeat above stay running.
        if data_not_ready:
            return

        candidate_symbols_by_timestamp: dict[datetime, set[str]] = {}
        skipped_by_symbol: dict[str, int] = {}
        for symbol, frame in frames.items():
            watermark = self._last_bar_ts.get(symbol)
            candidate_rows = (
                frame.iloc[[-1]] if watermark is None else frame.loc[frame["ts"] > watermark]
            )
            if not self._executor.simulation and len(candidate_rows) > 1:
                skipped_by_symbol[symbol] = len(candidate_rows) - 1
                candidate_rows = candidate_rows.iloc[[-1]]
            for raw_ts in candidate_rows["ts"]:
                timestamp = pd.Timestamp(raw_ts)
                if timestamp.tzinfo is None:
                    raise ValueError(f"{symbol} completed bar timestamp must be timezone-aware")
                event_ts = timestamp.to_pydatetime().astimezone(UTC)
                candidate_symbols_by_timestamp.setdefault(event_ts, set()).add(symbol)

        if skipped_by_symbol:
            summary = ", ".join(
                f"{symbol}={count}" for symbol, count in sorted(skipped_by_symbol.items())
            )
            logger.warning("Skipped superseded live bars: %s", summary)
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Live Catch-up Bars Skipped",
                message=f"Skipped older uncommitted bars ({summary}); only latest bars are tradable.",
            )

        held_from = self._held_cohort(frames, candidate_symbols_by_timestamp)
        for cycle_ts in sorted(candidate_symbols_by_timestamp):
            if held_from is not None and cycle_ts >= held_from:
                break
            if self._last_cycle_ts is not None and cycle_ts < self._last_cycle_ts:
                raise RuntimeError(
                    "out-of-order completed bar cannot be applied after a newer event: "
                    f"{cycle_ts} < {self._last_cycle_ts}"
                )

            event_frames: dict[str, pd.DataFrame] = {}
            advanced_symbols: list[str] = []
            event_symbols = set(candidate_symbols_by_timestamp[cycle_ts])
            if self._executor.simulation:
                event_symbols.update(
                    symbol
                    for symbol, frame in frames.items()
                    if bool((pd.to_datetime(frame["ts"], utc=True) == pd.Timestamp(cycle_ts)).any())
                )
            for symbol in sorted(event_symbols):
                frame = frames[symbol]
                event_frames[symbol] = frame
                if cycle_ts > self._last_bar_ts.get(
                    symbol,
                    datetime.min.replace(tzinfo=UTC),
                ):
                    advanced_symbols.append(symbol)

            if not advanced_symbols:
                continue
            logger.info(
                "New market-data event: ts=%s symbols=%s",
                cycle_ts,
                sorted(event_frames),
            )
            if self._batch_feature_fn is None:
                committed_feature_as_of = self._process_cycle(event_frames, cycle_ts)
            else:
                committed_feature_as_of = self._process_cycle(
                    frames,
                    cycle_ts,
                    active_symbols=advanced_symbols,
                )
                if committed_feature_as_of is None:
                    continue

            # Commit the event and feature frontiers together, only after the
            # full feature/strategy/execution cycle succeeded.
            for symbol in advanced_symbols:
                self._last_bar_ts[symbol] = cycle_ts
            self._last_cycle_ts = cycle_ts
            if committed_feature_as_of is not None:
                self._last_feature_as_of = committed_feature_as_of
            self._persist_state()
        self._trim_runtime_caches()

    def _held_cohort(
        self,
        frames: Mapping[str, pd.DataFrame],
        candidates: Mapping[datetime, set[str]],
    ) -> datetime | None:
        """The first event to hold back for a required symbol due it too (``COHORT_HOLD``).

        A symbol is due the event when the bar after its newest one closes
        by the event's close on its own calendar, so a market that is shut
        then does not hold the rest. Nothing is held when the next poll
        comes after the following bar closes: live then keeps only each
        symbol's newest bar and would skip the held one.
        """
        now = self._utc_now()
        next_poll = now + timedelta(seconds=self._poll_seconds)
        closes = {
            symbol: frame.set_index("ts")[AVAILABLE_AT_COLUMN] for symbol, frame in frames.items()
        }

        def next_close(symbol: str, after: pd.Timestamp) -> pd.Timestamp | None:
            subscription = self._market_data_subscriptions[symbol]
            try:
                return pd.Timestamp(
                    next_expected_close(
                        after.to_pydatetime(),
                        timeframe=subscription.timeframe,
                        calendar_id=subscription.calendar_id,
                    )
                )
            except ValueError:  # outside the calendar: nothing to wait for
                return None

        for event_ts in sorted(candidates):
            released = candidates[event_ts]
            event = pd.Timestamp(event_ts)
            event_close = min(closes[symbol][event] for symbol in released)
            if now >= event_close + self.COHORT_HOLD:
                continue
            following = [
                close for symbol in released if (close := next_close(symbol, event)) is not None
            ]
            if not following or next_poll >= min(following):
                continue
            late = []
            for symbol in frames:
                newest = pd.Timestamp(frames[symbol]["ts"].iloc[-1])
                if symbol in self._optional_symbols or symbol in released or newest >= event:
                    continue
                due = next_close(symbol, newest)
                if due is not None and due <= event_close:
                    late.append(symbol)
            if late:
                logger.info(
                    "Holding the %s bar of %s until %s have it or %s",
                    event_ts,
                    sorted(released),
                    sorted(late),
                    event_close + self.COHORT_HOLD,
                )
                return event_ts
        return None

    def _fetch_with_cache(self, symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV and keep a complete, deduplicated rolling cache."""
        try:
            return self._fetch_with_cache_unchecked(symbol)
        except Exception:
            logger.exception("Failed to fetch %s", symbol)
            return self._ohlcv_cache.get(symbol)

    def _fetch_with_cache_unchecked(
        self,
        symbol: str,
        *,
        audit_sink: list[_OhlcvAuditRow] | None = None,
        correction_sink: list[_OhlcvAuditRow] | None = None,
    ) -> pd.DataFrame | None:
        """Fetch and cache one symbol, propagating failures to the poll coordinator."""
        cached = self._ohlcv_cache.get(symbol)
        if cached is not None and not cached.empty:
            # Restored and test-injected legacy caches may predate row-version
            # metadata. Normalize them before comparing history fingerprints.
            cached = self._normalize_runtime_rows(symbol, cached)
        first_fill = cached is None or cached.empty
        if symbol in self._replay_backlog_exhausted:
            return cached
        if self._warmup_gap(symbol, cached) is not None:
            merged = cached
            base_request = self._warmup_periods + 1
            all_request_sizes = [
                base_request * (2**attempt) for attempt in range(self.WARMUP_MAX_FETCH_ATTEMPTS)
            ]
            request_sizes = all_request_sizes

            def merge_fetched(
                base: pd.DataFrame | None,
                fetched: pd.DataFrame,
            ) -> pd.DataFrame | None:
                fetched = self._eligible_runtime_rows(symbol, fetched)
                if fetched.empty:
                    return base
                validate_ohlcv_values(fetched, context=f"{symbol} runtime data")
                return self._merge_runtime_rows(
                    symbol,
                    base,
                    fetched,
                    audit_sink=audit_sink,
                    correction_sink=correction_sink,
                    first_fill=first_fill,
                )

            try:
                backoff = self._warmup_exhausted.get(symbol)
                if (
                    backoff is None
                    and self._warmup_requested_periods.get(symbol) == all_request_sizes[-1]
                ):
                    # A prior largest-rung response added history but did not
                    # close the gap. Re-probe that bound directly; exhaustion
                    # is only established when this rung itself plateaus.
                    request_sizes = [all_request_sizes[-1]]
                if backoff is not None:
                    request_sizes = []
                    # Newly closed bars are the ordinary increment, not history
                    # the largest request uncovered. Fetching them first keeps
                    # the cache fresh between re-probes and keeps them from
                    # counting as re-probe progress. The request widens until
                    # it reaches the cached head: warmup counts rows, so bars a
                    # failed or late poll missed would otherwise let a window
                    # with a hole turn ready.
                    head = None if merged is None or merged.empty else merged["ts"].iloc[-1]
                    # Adapters drop the forming bar from the requested count,
                    # so three rows reach back to the head after one new bar.
                    limit = 3
                    while True:
                        recent = self._eligible_runtime_rows(
                            symbol,
                            self._fetchers[symbol](
                                symbol, self._timeframe, limit, drop_incomplete=True
                            ),
                        )
                        reaches_head = head is None or recent.empty or recent["ts"].iloc[0] <= head
                        if reaches_head or limit >= all_request_sizes[-1]:
                            break
                        limit = min(2 * limit, all_request_sizes[-1])
                    # Merge only the widest response, and only if it bridges
                    # the hole or fills the window alone: merging a narrower
                    # one first would move the head past a hole that a failed
                    # wider request then leaves behind.
                    if reaches_head or len(recent) >= self._warmup_periods:
                        merged = merge_fetched(merged, recent)
                    polls_left, interval = backoff
                    if self._warmup_gap(symbol, merged) is None:
                        self._warmup_exhausted.pop(symbol)
                    elif polls_left > 1:
                        self._warmup_exhausted[symbol] = (polls_left - 1, interval)
                    else:
                        before = self._history_fingerprint(merged)
                        # Back off before walking, so a walk that fails
                        # (rate limit, a page error) is not retried every poll.
                        interval = min(2 * interval, self.WARMUP_REPROBE_MAX_INTERVAL_POLLS)
                        self._warmup_exhausted[symbol] = (interval, interval)
                        merged = merge_fetched(
                            merged,
                            self._fetch_history(symbol, all_request_sizes[-1]),
                        )
                        if self._warmup_gap(symbol, merged) is None:
                            self._warmup_exhausted.pop(symbol)
                        elif self._found_older_history(before, merged):
                            # A backfill may still be filling in: probe again
                            # next poll.
                            self._warmup_exhausted[symbol] = (1, 1)

                for requested_periods in request_sizes:
                    attempt = all_request_sizes.index(requested_periods) + 1
                    self._warmup_requested_periods[symbol] = requested_periods
                    self._warmup_fetch_attempts[symbol] = attempt
                    before = self._history_fingerprint(merged)
                    merged = merge_fetched(merged, self._fetch_history(symbol, requested_periods))
                    if self._warmup_gap(symbol, merged) is None:
                        break
                    if requested_periods == all_request_sizes[-1] and not (
                        self._found_older_history(before, merged)
                    ):
                        # Treat the plateau as an interval-1 re-probe, so the
                        # next waits two polls.
                        self._warmup_exhausted[symbol] = (2, 2)
                        break
            except Exception:
                self._store_runtime_cache(symbol, merged)
                raise
        else:
            self._warmup_exhausted.pop(symbol, None)
            new_df = self._fetchers[symbol](
                symbol,
                self._timeframe,
                2,
                drop_incomplete=True,
            )
            new_df = self._eligible_runtime_rows(symbol, new_df)
            if new_df.empty:
                return cached
            validate_ohlcv_values(new_df, context=f"{symbol} runtime data")
            merged = self._merge_runtime_rows(
                symbol,
                cached,
                new_df,
                audit_sink=audit_sink,
                correction_sink=correction_sink,
                first_fill=first_fill,
            )

        return self._store_runtime_cache(symbol, merged)

    def _store_runtime_cache(
        self,
        symbol: str,
        merged: pd.DataFrame | None,
    ) -> pd.DataFrame | None:
        """Persist one causal cache window, retaining pending simulation replay rows."""
        if merged is None or merged.empty:
            return merged
        watermark = self._last_bar_ts.get(symbol)
        has_unprocessed_sim_rows = (
            self._executor.simulation
            and watermark is not None
            and bool((pd.to_datetime(merged["ts"], utc=True) > watermark).any())
        )
        max_replay_rows = (self._warmup_periods + 1) * self.WARMUP_MAX_FETCH_MULTIPLIER
        if has_unprocessed_sim_rows and len(merged) > max_replay_rows:
            queued_rows = int((pd.to_datetime(merged["ts"], utc=True) > watermark).sum())
            self._replay_backlog_exhausted[symbol] = (queued_rows, max_replay_rows)
            logger.error(
                "Shadow replay backlog exceeded for %s: queued=%d max_history=%d; "
                "fetching and strategy evaluation are disabled until restart/resynchronization",
                symbol,
                queued_rows,
                max_replay_rows,
            )
            return self._ohlcv_cache.get(symbol)
        if not has_unprocessed_sim_rows:
            merged = merged.iloc[-self._warmup_periods :]
        merged = merged.reset_index(drop=True)
        self._ohlcv_cache[symbol] = merged
        head = pd.Timestamp(merged["ts"].iloc[0]).value
        received = self._bar_observed_at.get(symbol, {})
        self._bar_observed_at[symbol] = {
            version: at for version, at in received.items() if version[0] >= head
        }
        return merged

    def _fetch_history(self, symbol: str, requested_periods: int) -> pd.DataFrame:
        """Fetch one completed history window through the configured warmup route."""
        if self._warmup_fetcher:
            return self._warmup_fetcher(symbol, self._timeframe, requested_periods)
        return self._fetchers[symbol](
            symbol,
            self._timeframe,
            requested_periods,
            drop_incomplete=True,
        )

    @classmethod
    def _found_older_history(
        cls,
        before: frozenset[tuple[int, int]],
        merged: pd.DataFrame | None,
    ) -> bool:
        """Whether ``merged`` reaches back past the oldest bar in ``before``.

        Bars that closed during the walk or that an incremental fetch missed,
        and corrections (sources may stamp ``available_at`` differently), are
        merged but do not show a source filling in history.
        """
        oldest_before = min((ts for ts, _ in before), default=None)
        oldest_after = min((ts for ts, _ in cls._history_fingerprint(merged)), default=None)
        return oldest_after is not None and (oldest_before is None or oldest_after < oldest_before)

    @staticmethod
    def _history_fingerprint(
        frame: pd.DataFrame | None,
    ) -> frozenset[tuple[int, int]]:
        """Identify persisted row versions without treating them as extra history."""
        if frame is None or frame.empty:
            return frozenset()
        timestamps = pd.to_datetime(frame["ts"], utc=True)
        if AVAILABLE_AT_COLUMN in frame:
            availability = pd.to_datetime(frame[AVAILABLE_AT_COLUMN], utc=True)
        else:
            # Legacy/manual caches are normalized before storage. Keep their
            # pre-normalization fingerprint deterministic without inventing a
            # second observation identity.
            availability = timestamps
        return frozenset(
            (pd.Timestamp(ts).value, pd.Timestamp(available_at).value)
            for ts, available_at in zip(timestamps, availability, strict=True)
        )

    def _eligible_runtime_rows(self, symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
        """Normalize one exact subscription and expose only causally ready rows."""
        if frame.empty:
            return frame

        normalized = self._normalize_runtime_rows(symbol, frame)
        eligible = normalized[AVAILABLE_AT_COLUMN] <= pd.Timestamp(self._utc_now())
        return normalized.loc[eligible].copy()

    def _normalize_runtime_rows(
        self,
        symbol: str,
        frame: pd.DataFrame,
        *,
        subscription: MarketDataSubscription | None = None,
    ) -> pd.DataFrame:
        """Normalize one batch while retaining row-version audit metadata.

        ``subscription`` defaults to the symbol's executing cadence; an
        auxiliary input passes its own identity so bar times are normalized
        against the frequency they were actually sampled at.
        """
        if frame.empty:
            return frame.copy()

        if "ts" not in frame:
            raise ValueError(f"{symbol} market data requires a ts column")
        ordered = frame.copy()
        try:
            sort_timestamps = pd.DatetimeIndex(
                [pd.Timestamp(value).tz_convert("UTC") for value in ordered["ts"]]
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{symbol} bar timestamp must be valid and timezone-aware") from exc
        if sort_timestamps.hasnans:
            raise ValueError(f"{symbol} bar timestamps must not contain NaT")
        ordered["_librae_ts_sort"] = sort_timestamps
        ordered = ordered.sort_values("_librae_ts_sort", kind="stable").reset_index(drop=True)
        if ordered["_librae_ts_sort"].duplicated().any():
            raise ValueError(f"{symbol} runtime data requires unique bar timestamps")
        ordered = ordered.drop(columns="_librae_ts_sort")
        if subscription is None:
            subscription = self._market_data_subscriptions[symbol]
        timestamps, availability = normalize_bar_times(
            ordered["ts"],
            ordered.get(AVAILABLE_AT_COLUMN),
            subscription,
        )
        ordered["ts"] = timestamps
        ordered[AVAILABLE_AT_COLUMN] = availability
        return ordered

    def _merge_runtime_rows(
        self,
        symbol: str,
        cached: pd.DataFrame | None,
        fetched: pd.DataFrame,
        *,
        audit_sink: list[_OhlcvAuditRow] | None = None,
        correction_sink: list[_OhlcvAuditRow] | None = None,
        first_fill: bool = False,
    ) -> pd.DataFrame:
        """Merge causal row versions, then validate the whole cache cadence.

        ``first_fill`` marks a fetch into an empty cache. After a restart it
        offers ``audit_sink`` the window the last decision before it read, a
        best-effort backfill for rows a crash kept from the table; a decision
        records the bars it reads after that (``_offer_decision_window``).
        Older fetched bars are left out: a fetch can reach further back than
        any decision read, a row in the table reads as one a decision read,
        and a crash can keep only the last decision's window from the table.
        """
        fetched = self._normalize_runtime_rows(symbol, fetched)
        if cached is not None and not cached.empty:
            cached = self._normalize_runtime_rows(symbol, cached)
            revised = self._revised_in_place(symbol, cached, fetched)
            missing = cached.columns.difference(fetched.columns)
            if revised.any() and len(missing):
                # A revision keeps what the fetch lacks, such as a financing
                # rate whose endpoint failed this poll.
                carried = cached.set_index("ts").loc[fetched.loc[revised, "ts"], missing]
                carried.index = fetched.index[revised.to_numpy()]
                fetched = fetched.join(carried)
        else:
            revised = pd.Series(False, index=fetched.index)
        observed_at = self._record_observed_at(symbol, fetched)
        watermark = self._last_bar_ts.get(symbol)
        decided = watermark
        if decided is None and self._period_index:
            # The first decision's checkpoint lands before its watermark does;
            # its cycle's execution bar is the bar it decided on.
            decided = self._last_execution_bar_ts.get(symbol)

        def audit_rows(rows: pd.DataFrame) -> list[_OhlcvAuditRow]:
            return [
                (
                    pd.Timestamp(row["ts"]).to_pydatetime(),
                    {**row.drop(labels="ts").to_dict(), OBSERVED_AT_COLUMN: observed_at[index]},
                )
                for index, row in rows.iterrows()
            ]

        accepted_audit_rows: list[_OhlcvAuditRow] = []
        accepted_corrections: list[_OhlcvAuditRow] = []
        if audit_sink is not None and first_fill and decided is not None:
            read = fetched.loc[fetched["ts"] <= pd.Timestamp(decided)]
            accepted_audit_rows = audit_rows(read.iloc[-self._warmup_periods :])
        if cached is None or cached.empty:
            merged = fetched
        else:
            if correction_sink is not None and not first_fill and watermark is not None:
                previous_availability = cached.set_index("ts")[AVAILABLE_AT_COLUMN]
                previous_versions = fetched["ts"].map(previous_availability)
                later = previous_versions.notna() & (
                    fetched[AVAILABLE_AT_COLUMN] > pd.DatetimeIndex(previous_versions)
                )
                accepted_corrections = audit_rows(
                    fetched.loc[(later | revised) & (fetched["ts"] <= pd.Timestamp(watermark))]
                )
            # A revision replaces the version it revises; any other tie keeps
            # the cached row, whose extra columns a fetch may lack.
            cached = cached.loc[~cached["ts"].isin(fetched.loc[revised, "ts"])]
            cached = cached.assign(_librae_source_priority=0)
            fetched = fetched.assign(_librae_source_priority=1)
            merged = pd.concat([cached, fetched], ignore_index=True)
            merged = merged.sort_values(
                ["ts", AVAILABLE_AT_COLUMN, "_librae_source_priority"],
                ascending=[True, False, True],
                kind="stable",
            )
            merged = merged.drop_duplicates(subset="ts", keep="first").drop(
                columns="_librae_source_priority"
            )
        merged = merged.sort_values("ts", kind="stable").reset_index(drop=True)
        # Each fetch can be valid alone while overlapping a cached multi-bar
        # interval. Revalidate only after version selection has made the cache
        # deterministic.
        normalized = self._normalize_runtime_rows(symbol, merged)
        if audit_sink is not None:
            audit_sink.extend(accepted_audit_rows)
        if correction_sink is not None:
            correction_sink.extend(accepted_corrections)
        return normalized

    def _revised_in_place(
        self, symbol: str, cached: pd.DataFrame, fetched: pd.DataFrame
    ) -> pd.Series:
        """Fetched rows that change a cached bar's values under its ``available_at``.

        A source can revise a closed bar without moving the ``available_at``
        its calendar derives, so such a row is a new version of the bar, as
        ``run_bars`` keys it, and later windows read it. Its receipt time is
        reset so it records when this version arrived.
        """
        prior = cached.set_index("ts")
        revised = pd.Series(False, index=fetched.index)
        known = fetched.loc[fetched["ts"].isin(prior.index)]
        if known.empty:
            return revised
        before = prior.loc[known["ts"]].reset_index()
        after = known.reset_index(drop=True)
        changed = (
            before[AVAILABLE_AT_COLUMN].eq(after[AVAILABLE_AT_COLUMN])
            & before[list(OHLCV_COLUMNS)].ne(after[list(OHLCV_COLUMNS)]).any(axis=1)
        ).to_numpy()
        revised.loc[known.index[changed]] = True
        received = self._bar_observed_at.get(symbol, {})
        for (_, old), (_, new) in zip(
            before.loc[changed].iterrows(), after.loc[changed].iterrows(), strict=True
        ):
            received.pop(
                (pd.Timestamp(new["ts"]).value, pd.Timestamp(new[AVAILABLE_AT_COLUMN]).value), None
            )
            logger.warning(
                "%s bar %s changed under the same available_at %s: %s -> %s",
                symbol,
                new["ts"],
                new[AVAILABLE_AT_COLUMN],
                {column: old[column] for column in OHLCV_COLUMNS},
                {column: new[column] for column in OHLCV_COLUMNS},
            )
        return revised

    def _record_observed_at(self, symbol: str, fetched: pd.DataFrame) -> pd.Series:
        """Stamp each fetched row version with the first time it was received.

        A version fetched again on a later poll keeps its first stamp, so the
        audit row says when the engine had that version, not when it last
        asked for it.
        """
        now = self._utc_now()
        received = self._bar_observed_at.setdefault(symbol, {})
        return pd.Series(
            [
                received.setdefault((pd.Timestamp(ts).value, pd.Timestamp(available_at).value), now)
                for ts, available_at in zip(
                    fetched["ts"], fetched[AVAILABLE_AT_COLUMN], strict=True
                )
            ],
            index=fetched.index,
            dtype=object,
        )

    def _observed_at(self, symbol: str, ts: datetime, available_at: object | None) -> datetime:
        """When the engine received the cached version of one bar."""
        if available_at is not None:
            observed = self._bar_observed_at.get(symbol, {}).get(
                (pd.Timestamp(ts).value, pd.Timestamp(available_at).value)
            )
            if observed is not None:
                return observed
        # Every fetched row passes _merge_runtime_rows, so only a cache set
        # without fetching lacks a stamp; the decision time bounds it.
        logger.warning(
            "No receipt time recorded for %s at %s; recording the decision time", symbol, ts
        )
        return self._utc_now()

    def _commit_cycle_bars(
        self,
        audit_bars: Mapping[str, dict[str, object]],
        audit_histories: Mapping[str, pd.DataFrame],
        ts: datetime,
    ) -> None:
        """Checkpoint the cycle with its decided bars queued, then record them.

        Every return after a decision comes through here, a halt included: the
        caller advances the watermark past these bars, and nothing records a
        bar behind the watermark again. The window behind them is collected
        only now, after any order went out, so audit work never delays one.
        """
        try:
            self._queue_ohlcv(audit_bars, ts)
        except Exception:
            # A full queue reports each dropped row through the runtime-event
            # callback, which may raise; audit work must never stand between
            # a cycle, a halt included, and its checkpoint.
            logger.warning(
                "Queueing this cycle's bars failed; the checkpoint goes ahead without them",
                exc_info=True,
            )
        self._persist_state()
        try:
            self._deliver_ohlcv(audit_bars, ts)
            window_rows: list[tuple[str, datetime, dict[str, object]]] = []
            for symbol, history in audit_histories.items():
                window_rows.extend(self._unrecorded_window(symbol, history))
            self._offer_decision_window(window_rows)
        except Exception:
            # The checkpoint above holds the decided bars; a failure past it
            # (the flush's own checkpoint, or collecting the window) must not
            # turn a finished cycle, or a halt, into a failed one.
            logger.warning("Recording this cycle's bars failed after its checkpoint", exc_info=True)

    def _unrecorded_window(
        self, symbol: str, history: pd.DataFrame
    ) -> list[tuple[str, datetime, dict[str, object]]]:
        """Bars this decision reads that no earlier decision of the symbol did.

        ``history`` is the decision's window, indexed by ts and ending at the
        decided bar, which records itself. In steady state this is empty; it
        holds the window at a start, bars that arrived while warmup was short,
        and catch-up bars a live run stepped over.
        """
        previous = self._last_bar_ts.get(symbol)
        read = history.iloc[:-1]
        if previous is not None:
            read = read.loc[read.index > pd.Timestamp(previous)]
        rows: list[tuple[str, datetime, dict[str, object]]] = []
        for bar_ts, row in read.iterrows():
            bar = row.to_dict()
            bar[OBSERVED_AT_COLUMN] = self._observed_at(
                symbol, bar_ts, bar.get(AVAILABLE_AT_COLUMN)
            )
            rows.append((symbol, pd.Timestamp(bar_ts).to_pydatetime(), bar))
        return rows

    def _offer_decision_window(self, rows: list[tuple[str, datetime, dict[str, object]]]) -> None:
        """Offer a decision's unrecorded window once, after the decision."""
        if rows and self._durable_ohlcv_delivery and self._pending_ohlcv:
            # The sink just refused the decided bar; the window would only
            # wait on the same failing database.
            logger.warning(
                "Skipping %d window bar(s) of this decision: the OHLCV audit sink "
                "refused the decided bar",
                len(rows),
            )
            return
        self._offer_audit_rows(rows, "decision's window")

    def _offer_audit_rows(
        self, rows: list[tuple[str, datetime, dict[str, object]]], what: str
    ) -> None:
        """Offer best-effort audit rows, one batch per symbol when the sink takes batches.

        Not queued: a start's window or a restart's re-read can be thousands
        of rows, too many for the checkpoint and, one write each, enough to
        hold up a poll for minutes. A refusal is logged and the rest skipped
        rather than waiting on a dead sink.
        """
        if self._on_ohlcv is None or not rows:
            return
        batches: list[tuple[str, list[tuple[datetime, dict[str, object]]]]] = []
        in_batch: set[datetime] = set()
        for symbol, bar_ts, bar in rows:
            # A batch holds one version per bar; a second version opens the next.
            if not batches or batches[-1][0] != symbol or bar_ts in in_batch:
                batches.append((symbol, []))
                in_batch = set()
            batches[-1][1].append((bar_ts, bar))
            in_batch.add(bar_ts)
        for symbol, bars in batches:
            try:
                if self._on_ohlcv_batch is not None:
                    self._on_ohlcv_batch(symbol, self._timeframe, bars)
                else:
                    for bar_ts, bar in bars:
                        self._on_ohlcv(symbol, self._timeframe, bar, bar_ts)
            except Exception:
                logger.warning(
                    "OHLCV audit delivery of a %s failed for %s; the rest is not delivered",
                    what,
                    symbol,
                    exc_info=True,
                )
                return

    def _report_incomplete_warmup(self) -> None:
        """Publish an edge-triggered diagnostic after bounded backfill fails."""
        changed: list[str] = []
        for symbol in self._symbols:
            frame = self._ohlcv_cache.get(symbol)
            gap_reason = self._warmup_gap(symbol, frame)
            backlog = self._replay_backlog_exhausted.get(symbol)
            exhausted = symbol in self._warmup_exhausted
            if backlog is not None:
                reason = "warmup_replay_backlog_exhausted"
            elif gap_reason is not None and exhausted:
                reason = "warmup_backfill_exhausted"
            else:
                reason = gap_reason
            if reason is None:
                self._reported_warmup_reasons.pop(symbol, None)
                continue
            usable_periods = len(frame) if frame is not None else 0
            previous_reason = self._reported_warmup_reasons.get(symbol)
            if previous_reason == reason:
                continue
            self._reported_warmup_reasons[symbol] = reason
            requested_periods = self._warmup_requested_periods.get(symbol, 0)
            attempts = self._warmup_fetch_attempts.get(symbol, 0)
            missing_periods = self._warmup_missing_periods(symbol, frame)
            if backlog is not None:
                summary = f"{symbol} replay queue exceeded its {backlog[1]}-period history bound"
            elif gap_reason == "warmup_replay_history_incomplete":
                summary = (
                    f"{symbol} missing={missing_periods} periods before its next replay candidate "
                    f"(cached total={usable_periods}, requested up to {requested_periods})"
                )
            else:
                summary = (
                    f"{symbol} missing={missing_periods} periods "
                    f"(usable={usable_periods}/{self._warmup_periods}, "
                    f"requested up to {requested_periods})"
                )
            changed.append(summary)
            logger.warning(
                "Live warmup unavailable for %s: reason=%s missing=%d usable_total=%d "
                "required=%d requested=%d attempts=%d/%d; strategy evaluation remains disabled",
                symbol,
                reason,
                missing_periods,
                usable_periods,
                self._warmup_periods,
                requested_periods,
                attempts,
                self.WARMUP_MAX_FETCH_ATTEMPTS,
            )
            if self._on_runtime_event:
                self._on_runtime_event(
                    RuntimeEvent(
                        ts=self._utc_now(),
                        event_type="decision_skipped",
                        symbol=symbol,
                        detail={
                            "reason": reason,
                            "gap_reason": gap_reason,
                            "usable_periods": usable_periods,
                            "required_periods": self._warmup_periods,
                            "missing_periods": missing_periods,
                            "requested_periods": requested_periods,
                            "attempts": attempts,
                            "max_attempts": self.WARMUP_MAX_FETCH_ATTEMPTS,
                            "terminal": exhausted or backlog is not None,
                            **(
                                {
                                    "queued_replay_periods": backlog[0],
                                    "max_history_periods": backlog[1],
                                }
                                if backlog is not None
                                else {}
                            ),
                        },
                    )
                )
        if changed:
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Live Warmup Incomplete",
                message=(
                    ", ".join(changed)
                    + "; strategy evaluation is disabled; exhausted sources are re-probed "
                    "at growing intervals, while replay overflow requires "
                    "restart/resynchronization"
                ),
            )

    def _warmup_missing_periods(
        self,
        symbol: str,
        frame: pd.DataFrame | None,
    ) -> int:
        """Return the actual feature-history shortfall at the next event."""
        if frame is None or len(frame) < self._warmup_periods:
            return self._warmup_periods - (len(frame) if frame is not None else 0)
        if not self._executor.simulation:
            return 0
        watermark = self._last_bar_ts.get(symbol)
        if watermark is None:
            return 0
        candidate_positions = pd.to_datetime(frame["ts"], utc=True) > watermark
        if not bool(candidate_positions.any()):
            return 0
        first_position = int(candidate_positions.to_numpy().argmax())
        return max(0, self._warmup_periods - first_position - 1)

    def _warmup_gap(self, symbol: str, frame: pd.DataFrame | None) -> str | None:
        """Return why a symbol cannot safely evaluate its next runtime event."""
        if frame is None or len(frame) < self._warmup_periods:
            return "warmup_incomplete"
        if not self._executor.simulation:
            return None
        watermark = self._last_bar_ts.get(symbol)
        if watermark is None:
            return None
        timestamps = pd.to_datetime(frame["ts"], utc=True)
        candidate_positions = timestamps > watermark
        if not bool(candidate_positions.any()):
            return None
        first_position = int(candidate_positions.to_numpy().argmax())
        if first_position < self._warmup_periods - 1:
            return "warmup_replay_history_incomplete"
        return None

    def _trim_runtime_caches(self) -> None:
        """Return temporary simulation replay windows to configured retention."""
        for symbol, frame in self._ohlcv_cache.items():
            if len(frame) > self._warmup_periods:
                self._ohlcv_cache[symbol] = frame.iloc[-self._warmup_periods :].reset_index(
                    drop=True
                )

    def _publish_action_results(self, result: ExecutionResult) -> None:
        """Publish notifications and analytics after state is committed."""
        fills: list[dict[str, object]] = []

        for event in result.events:
            self._event_sequence += 1
            self._pending_traded_notional += abs(event.notional)
            logger.info(
                "Order event: %s %s %s %.4f @ %.2f",
                event.event_type,
                event.side,
                event.symbol,
                event.fill_quantity,
                event.price,
            )
            if event.event_type in ("close", "reduce"):
                self._performance_dirty = True
            if self._on_position_event:
                if self._durable_position_event_delivery:
                    self._queue_position_event(event, self._event_sequence)
                else:
                    self._on_position_event(event, self._event_sequence)

            if event.event_type in ("open", "add"):
                label = (
                    event.side.upper()
                    if event.event_type == "open"
                    else f"{event.side.upper()} ADD"
                )
                logger.info("SIGNAL %s %s @ %.2f", label, event.symbol, event.price)
                fills.append(
                    {
                        "type": "entry",
                        "symbol": event.symbol,
                        "side": label,
                        "price": event.price,
                        "quantity": event.fill_quantity,
                        "notional": event.notional,
                    }
                )

        self._flush_pending_position_events()

        for trade in result.trades:
            logger.info("SIGNAL EXIT %s @ %.2f", trade.symbol, trade.exit_price)
            fills.append(
                {
                    "type": "exit",
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "net_pnl": trade.net_pnl,
                    "net_return": trade.net_return,
                    "periods_held": trade.periods_held,
                }
            )
            logger.info("Position closed: %s @ %.2f", trade.symbol, trade.exit_price)

        if len(fills) > self.SIGNAL_BATCH_THRESHOLD:
            self._notify(
                "send_batch",
                strategy=self._executor.strategy_name,
                fills=fills,
            )
            return

        for fill in fills:
            if fill["type"] == "entry":
                self._notify(
                    "send_signal",
                    strategy=self._executor.strategy_name,
                    symbol=fill["symbol"],
                    side=fill["side"],
                    price=fill["price"],
                    quantity=fill["quantity"],
                    notional=fill["notional"],
                )
            else:
                self._notify(
                    "send_exit",
                    strategy=self._executor.strategy_name,
                    symbol=fill["symbol"],
                    side=fill["side"],
                    entry_price=fill["entry_price"],
                    exit_price=fill["exit_price"],
                    net_pnl=fill["net_pnl"],
                    net_return=fill["net_return"],
                    periods_held=fill["periods_held"],
                )

        if self._on_runtime_event:
            for runtime_event in result.runtime_events:
                self._on_runtime_event(runtime_event)

    def _queue_position_event(self, event: PositionEvent, sequence: int) -> None:
        pending = self._pending_position_events
        pending.append((event, sequence))
        if len(pending) > self.MAX_PENDING_POSITION_EVENTS:
            # A result with many fills fills the queue before its own flush,
            # so offer it first and drop only what a refusing sink left.
            self._flush_pending_position_events()
        while len(pending) > self.MAX_PENDING_POSITION_EVENTS:
            self._drop_position_event(*pending[0], reason="backlog_full")
            self._pop_head_position_event()

    def _drop_position_event(self, event: PositionEvent, sequence: int, *, reason: str) -> None:
        """Drop one undelivered event loudly, without stopping the book or raising.

        The checkpoint and the broker remain the fill's record, so this loses
        an analytics row, not position truth. The sequence is kept in memory
        and logged before the runtime event is tried, because that sink often
        writes to the store that just refused the event; its failure is logged
        and swallowed so it cannot interrupt queueing or order work.
        """
        self._dropped_position_event_sequences.append(sequence)
        logger.error(
            "Dropped undelivered position event %d (%s %s at %s): %s",
            sequence,
            event.symbol,
            event.event_type,
            event.ts.isoformat(),
            reason,
        )
        self._mark_position_event_delivery_degraded(f"position event {sequence} dropped ({reason})")
        if not self._on_runtime_event:
            return
        try:
            self._on_runtime_event(
                RuntimeEvent(
                    ts=self._utc_now(),
                    event_type="decision_skipped",
                    symbol=event.symbol,
                    detail={
                        "reason": "position_event_delivery_failed",
                        "drop_reason": reason,
                        "sequence": sequence,
                        "event_type": event.event_type,
                        "ts": event.ts.isoformat(),
                    },
                )
            )
        except Exception as exc:
            logger.warning("Runtime event for dropped position event %d failed: %s", sequence, exc)

    def _mark_position_event_delivery_degraded(self, cause: str) -> None:
        """Flag and alert once per episode, at the first failure rather than the first drop.

        Waiting for a drop would leave a sink that refuses one event silent
        while fills pile up behind it, which on a slow book can take months.
        """
        self._clean_position_event_cycles = 0
        if self._position_event_delivery_degraded:
            return
        self._position_event_delivery_degraded = True
        logger.error("Position event delivery failing: %s", cause)
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Position Event Delivery Failing",
            message=(
                f"{cause}. Undelivered events are retried every cycle; one refused for "
                f"{self.MAX_POSITION_EVENT_ATTEMPTS} cycles, or beyond "
                f"{self.MAX_PENDING_POSITION_EVENTS} queued, is dropped and its sequence "
                "logged. Trading continues — the checkpoint and broker still hold every fill."
            ),
        )

    def _pop_head_position_event(self) -> None:
        self._pending_position_events.pop(0)
        self._head_position_event_failures = 0

    def _flush_pending_position_events(self, *, count_attempt: bool = False) -> None:
        """Offer queued position events in sequence order, keeping any not accepted.

        Only the poll cycle's flush counts toward the head's attempt bound and
        toward recovery, so a cycle with many fills cannot use either up.
        """
        while self._pending_position_events:
            event, sequence = self._pending_position_events[0]
            try:
                self._on_position_event(event, sequence)
            except Exception as exc:
                logger.warning(
                    "Position event %d delivery failed (%s); %d event(s) still pending",
                    sequence,
                    exc,
                    len(self._pending_position_events),
                )
                self._mark_position_event_delivery_degraded(
                    f"position event {sequence} refused ({type(exc).__name__}: {exc})"
                )
                if not count_attempt:
                    return
                self._head_position_event_failures += 1
                if self._head_position_event_failures >= self.MAX_POSITION_EVENT_ATTEMPTS:
                    self._drop_position_event(event, sequence, reason="attempts_exhausted")
                    self._pop_head_position_event()
                # The next event gets its first attempt in a later cycle, so an
                # outage drops at most one event per bound's worth of cycles.
                return
            self._pop_head_position_event()
        if not count_attempt or not self._position_event_delivery_degraded:
            return
        self._clean_position_event_cycles += 1
        if self._clean_position_event_cycles >= self.POSITION_EVENT_RECOVERY_CYCLES:
            self._position_event_delivery_degraded = False
            logger.info("Position event delivery recovered")

    def _commit_simulated_results(
        self,
        *,
        cash: float,
        positions: dict[str, PositionState],
        result: ExecutionResult,
    ) -> None:
        """Commit a deterministic simulated fill batch."""
        self._cash = cash
        self._positions = positions
        self._trade_count += len(result.trades)
        self._publish_action_results(result)

    def _prepare_live_order(
        self,
        request: OrderRequest,
        *,
        reference_price: float,
    ) -> OrderRequest:
        """Apply venue validation, then enforce the live limit-price collar.

        The executor already rejects a prepared order that changes symbol,
        drops or changes a validated limit price, or enlarges the quantity.
        Adapter-owned variable tick rules therefore fail closed instead of
        silently changing strategy intent. The opt-in
        ``max_limit_price_deviation_rate`` collar below additionally bounds
        the limit against the completed-bar reference price.
        """
        prepared = self._executor.prepare_order(
            request,
            reference_price=reference_price,
        )
        max_deviation = self._risk_policy.max_limit_price_deviation_rate
        if prepared.limit_price is None or max_deviation is None:
            return prepared
        deviation = abs(prepared.limit_price - reference_price) / reference_price
        if deviation > max_deviation + EPSILON:
            raise ValueError(
                f"{prepared.symbol} limit price {prepared.limit_price:.6f} is "
                f"{deviation:.2%} from reference {reference_price:.6f}, exceeding "
                f"max_limit_price_deviation_rate={max_deviation:.2%}"
            )
        return prepared

    def _report_group_preflight_rejection(
        self,
        group_id: str,
        actions: list[OrderIntent],
        ts: datetime,
        error: ValueError,
    ) -> None:
        """Report one locally rejected group without halting unrelated work."""
        symbols = [action.symbol or self._symbols[0] for action in actions]
        message = f"group {group_id!r} rejected before submission: {error}"
        logger.error("Live group preflight rejected: %s", message)
        if self._on_runtime_event:
            self._on_runtime_event(
                RuntimeEvent(
                    ts=ts,
                    event_type="decision_skipped",
                    detail={
                        "reason": "group_preflight_rejected",
                        "group_id": group_id,
                        "symbols": symbols,
                        "message": str(error),
                    },
                )
            )
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Live Group Preflight Rejected",
            message=f"{message}; no leg was submitted, unrelated orders remain eligible.",
        )

    def _report_skipped_close(
        self,
        symbol: str,
        *,
        quantity: float,
        held: float | None,
        ts: datetime,
        reason: str,
        title: str,
        cause: str,
        others: str = "other orders remain eligible",
    ) -> None:
        """Record a skipped exit every time; alert once per reason and held quantity.

        A strategy that keeps closing the same remainder would otherwise page
        the operator every bar about a position that has not changed.
        ``held`` is None once the position is no longer in the ledger.
        """
        logger.warning("%s skipped for %s: %s", title, symbol, cause)
        if self._on_runtime_event:
            self._on_runtime_event(
                RuntimeEvent(
                    ts=ts,
                    event_type="decision_skipped",
                    symbol=symbol,
                    detail={"reason": reason, "quantity": quantity, "message": cause},
                )
            )
        held_quantity = 0.0 if held is None else held
        alerted = self._skipped_close_alerts.get((reason, symbol))
        if alerted is not None and isclose(alerted, held_quantity, rel_tol=0.0, abs_tol=EPSILON):
            return
        self._skipped_close_alerts[(reason, symbol)] = held_quantity
        remaining = (
            "the position is no longer in the ledger"
            if held is None
            else f"{held:g} stays open in the ledger"
        )
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] {title}: {symbol}",
            message=(f"exit of {quantity:g} skipped: {cause}; {remaining}, {others}."),
        )

    def _plan_live_orders(
        self,
        intent: StrategyDecision,
        bars: dict[str, dict[str, float]],
        ts: datetime,
        *,
        apply_volume_limit: bool = True,
        apply_entry_risk_limits: bool = True,
        lagged_adv_by_symbol: dict[str, float] | None = None,
        used_bar_quantity_by_symbol: dict[str, float] | None = None,
        sequence_start: int = 0,
        withheld_entries: set[str] | None = None,
    ) -> list[OrderRequest]:
        """Size intent at the latest completed close without inventing fills.

        Given ``withheld_entries``, a target plans only its reductions and adds
        the symbols it would open or add to that set.
        """
        primary_symbol = self._symbols[0]
        prices = {
            symbol: float(bar["close"])
            for symbol, bar in bars.items()
            if bar.get("close") is not None and float(bar["close"]) > 0
        }
        exposure_prices = dict(self._last_prices)
        exposure_prices.update(prices)
        unavailable_side_symbols: set[str] = set()

        def get_price(symbol: str, action: OrderIntent) -> float | None:
            price = prices.get(symbol)
            if price is None:
                return None
            if action.action == "long":
                order_side: Literal["buy", "sell"] | None = "buy"
            elif action.action == "short":
                order_side = "sell"
            else:
                position = staged_positions.get(symbol)
                order_side = (
                    None if position is None else "sell" if position.side == "long" else "buy"
                )
            if order_side is not None and not order_side_is_tradable(
                bars.get(symbol, {}), order_side
            ):
                unavailable_side_symbols.add(symbol)
                return None
            return price

        def get_volume(symbol: str) -> float | None:
            volume = bars.get(symbol, {}).get("volume")
            return float(volume) if volume is not None else None

        max_order_notional = (
            self._risk_policy.max_order_notional if apply_entry_risk_limits else None
        )
        max_position_notional = None
        if apply_entry_risk_limits and self._risk_policy.max_position_weight:
            execution_equity, _ = calc_equity(
                self._cash,
                self._positions,
                get_price=lambda symbol, _position: exposure_prices[symbol],
                get_cost_model=self._get_cost_model,
            )
            max_position_notional = self._risk_policy.max_position_weight * max(
                execution_equity, 0.0
            )
        volume_limit = self._max_bar_volume_participation_rate if apply_volume_limit else None
        adv_limit = self._max_adv_participation_rate if apply_volume_limit else None
        staged_positions = deepcopy(self._positions)
        planned_bar_quantity_by_symbol = dict(used_bar_quantity_by_symbol or {})
        planned_adv_quantity_by_symbol = dict(self._adv_filled_quantities)
        lagged_adv = lagged_adv_by_symbol or {}
        prepared_positions = deepcopy(self._positions)
        prepared_cash = self._cash
        prepared_exposure_prices = dict(exposure_prices)
        prepared_bar_quantity_by_symbol = dict(used_bar_quantity_by_symbol or {})
        prepared_adv_quantity_by_symbol = dict(self._adv_filled_quantities)

        def prepare_and_validate(
            request: OrderRequest,
            *,
            reference_price: float,
        ) -> OrderRequest:
            nonlocal prepared_cash
            prepared = self._prepare_live_order(
                request,
                reference_price=reference_price,
            )
            risk_price = prepared.limit_price or reference_price
            positions_before = deepcopy(prepared_positions)
            cash_before = prepared_cash
            if prepared.position_effect in ("open", "add"):
                action = "long" if prepared.side == "buy" else "short"
            else:
                action = "close"
            validation_result = execute_order_intents(
                [
                    OrderIntent(
                        action=action,
                        symbol=prepared.symbol,
                        quantity=prepared.quantity,
                        reason=prepared.reason,
                        limit_price=(
                            prepared.limit_price if prepared.order_type == "limit" else None
                        ),
                        group_id=prepared.group_id,
                    )
                ],
                prepared_positions,
                prepared_cash,
                ts,
                get_price=lambda _symbol, _action: risk_price,
                get_cost_model=self._get_cost_model,
                primary_symbol=primary_symbol,
                max_position_notional=max_position_notional,
                max_order_notional=max_order_notional,
                max_bar_volume_participation_rate=volume_limit,
                max_adv_participation_rate=adv_limit,
                get_volume=get_volume,
                get_lagged_adv=lambda symbol: lagged_adv.get(symbol),
                used_bar_quantity_by_symbol=prepared_bar_quantity_by_symbol,
                used_adv_quantity_by_symbol=prepared_adv_quantity_by_symbol,
                get_executable_quantity=self._get_executable_quantity,
                validate_intent_prices=self._validate_intent_prices,
                get_min_notional=self._get_min_notional,
            )
            executable_quantity = sum(event.fill_quantity for event in validation_result.events)
            if abs(executable_quantity - prepared.quantity) > EPSILON:
                reasons = [
                    str(event.detail["reason"])
                    for event in validation_result.runtime_events
                    if "reason" in event.detail
                ]
                reason = reasons[0] if reasons else "quantity_capped"
                raise ValueError(
                    f"{prepared.symbol} prepared order failed post-adapter risk "
                    f"validation: quantity={prepared.quantity:.6f}, "
                    f"executable={executable_quantity:.6f}, reason={reason}"
                )

            prepared_cash += validation_result.cash_delta
            prepared_exposure_prices[prepared.symbol] = risk_price
            if apply_entry_risk_limits:
                validate_exposure_transition(
                    positions_before=positions_before,
                    cash_before=cash_before,
                    positions_after=prepared_positions,
                    cash_after=prepared_cash,
                    prices=prepared_exposure_prices,
                    get_cost_model=self._get_cost_model,
                    max_gross_exposure=self._risk_policy.max_gross_exposure,
                    max_net_exposure=self._risk_policy.max_net_exposure,
                )
            return prepared

        if isinstance(intent, PortfolioWeights):
            try:
                result = execute_portfolio_weights(
                    intent,
                    staged_positions,
                    self._cash,
                    ts,
                    get_price=get_price,
                    get_reference_price=lambda symbol: prices.get(symbol),
                    get_cost_model=self._get_cost_model,
                    primary_symbol=primary_symbol,
                    max_position_notional=max_position_notional,
                    max_order_notional=max_order_notional,
                    max_bar_volume_participation_rate=volume_limit,
                    max_adv_participation_rate=adv_limit,
                    get_volume=get_volume,
                    get_lagged_adv=lambda symbol: lagged_adv.get(symbol),
                    used_bar_quantity_by_symbol=planned_bar_quantity_by_symbol,
                    used_adv_quantity_by_symbol=planned_adv_quantity_by_symbol,
                    get_executable_quantity=self._get_executable_quantity,
                    get_min_notional=self._get_min_notional,
                    reductions_only=withheld_entries is not None,
                )
            except ExecutionPriceUnavailableError as exc:
                if unavailable_side_symbols:
                    raise ExecutionSideUnavailableError(sorted(unavailable_side_symbols)) from exc
                raise
            if withheld_entries is not None and result.pending_rebalance is not None:
                withheld = result.pending_rebalance
                withheld_entries.update(order.intent.symbol for order in withheld.orders)
                withheld_entries.update(leg.symbol for leg in withheld.unresolved_legs)
            temporarily_blocked = sorted(
                {
                    event.symbol
                    for event in result.runtime_events
                    if event.symbol is not None
                    and event.detail.get("reason") in ("volume_capped", "volume_unavailable")
                }
            )
            if not result.events and temporarily_blocked:
                raise ExecutionLiquidityUnavailableError(temporarily_blocked)
            if self._on_runtime_event:
                for runtime_event in result.runtime_events:
                    self._on_runtime_event(runtime_event)
            if apply_entry_risk_limits:
                replay_positions = deepcopy(self._positions)
                replay_cash = self._cash
                replay_prices = dict(exposure_prices)
                for event in result.events:
                    positions_before_event = deepcopy(replay_positions)
                    cash_before_event = replay_cash
                    is_entry = event.event_type in ("open", "add")
                    order_side: Literal["buy", "sell"]
                    if is_entry:
                        order_side = "buy" if event.side == "long" else "sell"
                    else:
                        order_side = "sell" if event.side == "long" else "buy"
                    replay_cash, _ = apply_execution_fill(
                        replay_positions,
                        replay_cash,
                        Fill(
                            symbol=event.symbol,
                            side=event.side,
                            price=event.price,
                            quantity=event.fill_quantity,
                            commission=event.commission,
                            slippage=event.slippage,
                            tax=event.tax,
                        ),
                        ts,
                        order_side=order_side,
                        cost_model=self._get_cost_model(event.symbol),
                        reason=event.reason,
                        group_id=event.group_id,
                        time_in_force=event.time_in_force,
                    )
                    replay_prices[event.symbol] = event.price
                    validate_exposure_transition(
                        positions_before=positions_before_event,
                        cash_before=cash_before_event,
                        positions_after=replay_positions,
                        cash_after=replay_cash,
                        prices=replay_prices,
                        get_cost_model=self._get_cost_model,
                        max_gross_exposure=self._risk_policy.max_gross_exposure,
                        max_net_exposure=self._risk_policy.max_net_exposure,
                    )
                validate_exposure_transition(
                    positions_before=self._positions,
                    cash_before=self._cash,
                    positions_after=staged_positions,
                    cash_after=self._cash + result.cash_delta,
                    prices=exposure_prices,
                    get_cost_model=self._get_cost_model,
                    max_gross_exposure=self._risk_policy.max_gross_exposure,
                    max_net_exposure=self._risk_policy.max_net_exposure,
                )
            requests = []
            for index, event in enumerate(result.events):
                request = self._executor.request_from_event(
                    event,
                    sequence=sequence_start + index,
                )
                requests.append(
                    prepare_and_validate(
                        request,
                        reference_price=prices[event.symbol],
                    )
                )
            return requests

        actions = intent
        units: list[tuple[str | None, list[OrderIntent]]] = []
        grouped_units: dict[str, list[OrderIntent]] = {}
        for action in actions:
            group_id = action.group_id
            if group_id is None:
                units.append((None, [action]))
                continue
            if group_id not in grouped_units:
                grouped_units[group_id] = []
                units.append((group_id, grouped_units[group_id]))
            grouped_units[group_id].append(action)

        requests: list[OrderRequest] = []
        planning_cash = self._cash
        planning_exposure_prices = dict(exposure_prices)
        for group_id, unit_actions in units:
            grouped = group_id is not None
            # Staged on copies whenever the unit can be dropped: a group, or a
            # close/reduce, whose exit may be skipped. Only a close intent plans
            # an exit (an opposite-side entry is refused), so entries commit in
            # place as they always have.
            isolated = grouped or any(action.action == "close" for action in unit_actions)
            unit_positions = deepcopy(staged_positions) if isolated else staged_positions
            unit_cash = planning_cash
            unit_exposure_prices = (
                dict(planning_exposure_prices) if isolated else planning_exposure_prices
            )
            unit_bar_quantities = (
                dict(planned_bar_quantity_by_symbol) if isolated else planned_bar_quantity_by_symbol
            )
            unit_adv_quantities = (
                dict(planned_adv_quantity_by_symbol) if isolated else planned_adv_quantity_by_symbol
            )
            unit_skipped = False
            unit_requests: list[OrderRequest] = []
            preparation_scales: list[float] = []
            prepared_positions_before_unit = (
                deepcopy(prepared_positions) if grouped else prepared_positions
            )
            prepared_cash_before_unit = prepared_cash
            prepared_exposure_prices_before_unit = (
                dict(prepared_exposure_prices) if grouped else prepared_exposure_prices
            )
            prepared_bar_quantities_before_unit = (
                dict(prepared_bar_quantity_by_symbol)
                if grouped
                else prepared_bar_quantity_by_symbol
            )
            prepared_adv_quantities_before_unit = (
                dict(prepared_adv_quantity_by_symbol)
                if grouped
                else prepared_adv_quantity_by_symbol
            )

            try:
                unit_actions, normalization_events = normalize_order_intents(
                    unit_actions,
                    ts,
                    primary_symbol=primary_symbol,
                    get_executable_quantity=self._get_executable_quantity,
                )
                if self._on_runtime_event:
                    for runtime_event in normalization_events:
                        self._on_runtime_event(runtime_event)
                for action in unit_actions:
                    if action.stop_price is not None or action.take_profit_price is not None:
                        raise ValueError(
                            "Live stop-loss/take-profit requires broker-native protective "
                            "orders; completed-bar range checks are simulation-only"
                        )
                    symbol = action.symbol or primary_symbol
                    reference_price = prices.get(symbol)
                    # WHY: a group asks for fill-or-kill, so a leg that cannot
                    # be planned fails the group. An ungrouped intent keeps the
                    # simulated semantics -- a missing price or a close with
                    # nothing to close is skipped, not a halt -- because a
                    # close can legitimately arrive after its position is
                    # already gone (idempotent close, restart drift).
                    if reference_price is None:
                        if not grouped:
                            continue
                        raise ValueError(f"{symbol} has no positive live reference price")
                    if grouped and action.action != "close" and action.quantity is None:
                        raise ValueError(
                            f"{symbol} grouped entry intent requires an explicit quantity"
                        )

                    if action.action == "long":
                        order_side = "buy"
                    elif action.action == "short":
                        order_side = "sell"
                    else:
                        position = unit_positions.get(symbol)
                        if position is None:
                            if not grouped:
                                continue
                            raise ValueError(f"{symbol} close intent has no open position")
                        order_side = "sell" if position.side == "long" else "buy"
                    if not order_side_is_tradable(bars.get(symbol, {}), order_side):
                        if grouped:
                            raise ValueError(
                                f"{symbol} {order_side} side is not tradable on the decision bar"
                            )
                        if self._on_runtime_event:
                            self._on_runtime_event(
                                RuntimeEvent(
                                    ts=ts,
                                    event_type="decision_skipped",
                                    symbol=symbol,
                                    detail={
                                        "reason": "side_not_tradable",
                                        "side": order_side,
                                    },
                                )
                            )
                        continue

                    order_type = "limit" if action.limit_price is not None else "market"
                    limit_price = action.limit_price
                    expected_quantity = (
                        unit_positions[symbol].quantity
                        if grouped and action.action == "close" and action.quantity is None
                        else action.quantity
                    )
                    planning_action = replace(
                        action,
                        quantity=expected_quantity,
                    )
                    positions_before_action = deepcopy(unit_positions)
                    cash_before_action = unit_cash
                    result = execute_order_intents(
                        [planning_action],
                        unit_positions,
                        unit_cash,
                        ts,
                        get_price=lambda requested_symbol, _action, _symbol=symbol, _limit=limit_price: (
                            _limit
                            if requested_symbol == _symbol and _limit is not None
                            else prices.get(requested_symbol)
                        ),
                        get_cost_model=self._get_cost_model,
                        primary_symbol=primary_symbol,
                        max_position_notional=max_position_notional,
                        max_order_notional=max_order_notional,
                        max_bar_volume_participation_rate=volume_limit,
                        max_adv_participation_rate=adv_limit,
                        get_volume=get_volume,
                        get_lagged_adv=lambda requested_symbol: lagged_adv.get(requested_symbol),
                        used_bar_quantity_by_symbol=unit_bar_quantities,
                        used_adv_quantity_by_symbol=unit_adv_quantities,
                        get_executable_quantity=self._get_executable_quantity,
                        validate_intent_prices=self._validate_intent_prices,
                        get_min_notional=self._get_min_notional,
                    )
                    if not grouped and self._on_runtime_event:
                        for runtime_event in result.runtime_events:
                            self._on_runtime_event(runtime_event)
                    if grouped and len(result.events) != 1:
                        reasons = sorted(
                            {
                                str(event.detail.get("reason", "unplannable"))
                                for event in result.runtime_events
                            }
                        )
                        detail = ", ".join(reasons) if reasons else "no executable position delta"
                        raise ValueError(f"{symbol} intent could not be planned: {detail}")
                    if not result.events:
                        continue
                    event = result.events[0]
                    if grouped and not isclose(
                        event.fill_quantity,
                        expected_quantity,
                        rel_tol=0.0,
                        abs_tol=EPSILON,
                    ):
                        raise ValueError(
                            f"{symbol} planned quantity {event.fill_quantity:.6f} does not "
                            f"fully satisfy requested quantity {expected_quantity:.6f}"
                        )

                    unit_cash += result.cash_delta
                    unit_exposure_prices[event.symbol] = event.price
                    if apply_entry_risk_limits:
                        validate_exposure_transition(
                            positions_before=positions_before_action,
                            cash_before=cash_before_action,
                            positions_after=unit_positions,
                            cash_after=unit_cash,
                            prices=unit_exposure_prices,
                            get_cost_model=self._get_cost_model,
                            max_gross_exposure=self._risk_policy.max_gross_exposure,
                            max_net_exposure=self._risk_policy.max_net_exposure,
                        )

                    request = self._executor.request_from_event(
                        event,
                        order_type=order_type,
                        limit_price=limit_price,
                        sequence=sequence_start + len(requests) + len(unit_requests),
                    )
                    try:
                        prepared = prepare_and_validate(
                            request,
                            reference_price=reference_price,
                        )
                    except OrderBelowVenueMinimumError as exc:
                        # WHY: the venue refuses this size outright, so
                        # failing the batch would only strand every other
                        # exit behind a remainder it cannot close.
                        if grouped or request.position_effect not in ("reduce", "close"):
                            raise
                        self._report_skipped_close(
                            symbol,
                            quantity=request.quantity,
                            held=positions_before_action[symbol].quantity,
                            ts=ts,
                            reason="close_below_venue_minimum",
                            title="Close Below Venue Minimum",
                            cause=str(exc),
                        )
                        assert isolated, "a skipped exit must be staged on copies"
                        unit_skipped = True
                        break
                    preparation_scales.append(prepared.quantity / request.quantity)
                    unit_requests.append(prepared)

                if grouped and preparation_scales:
                    first_scale = preparation_scales[0]
                    if any(
                        not isclose(scale, first_scale, rel_tol=EPSILON, abs_tol=EPSILON)
                        for scale in preparation_scales[1:]
                    ):
                        raise ValueError(
                            "adapter quantity normalization changes relative leg ratios: "
                            + ", ".join(f"{scale:.8f}" for scale in preparation_scales)
                        )
            except ValueError as exc:
                if not grouped:
                    raise
                assert group_id is not None
                prepared_positions = prepared_positions_before_unit
                prepared_cash = prepared_cash_before_unit
                prepared_exposure_prices = prepared_exposure_prices_before_unit
                prepared_bar_quantity_by_symbol = prepared_bar_quantities_before_unit
                prepared_adv_quantity_by_symbol = prepared_adv_quantities_before_unit
                self._report_group_preflight_rejection(group_id, unit_actions, ts, exc)
                continue
            if unit_skipped:
                continue

            staged_positions = unit_positions
            planning_cash = unit_cash
            planning_exposure_prices = unit_exposure_prices
            planned_bar_quantity_by_symbol = unit_bar_quantities
            planned_adv_quantity_by_symbol = unit_adv_quantities
            requests.extend(unit_requests)
        return requests

    def _execute_live_decision(
        self,
        intent: StrategyDecision,
        bars: dict[str, dict[str, float]],
        ts: datetime,
        *,
        apply_volume_limit: bool = True,
        lagged_adv_by_symbol: dict[str, float] | None = None,
    ) -> bool:
        """Persist a deterministic order queue, then advance it serially."""
        intent = self._without_halted_account(intent)
        if not intent:
            return True

        if isinstance(intent, PortfolioWeights):
            if self._live_rebalance is not None:
                raise RuntimeError("cannot replace an active live rebalance")
            self._live_rebalance = LiveRebalance(
                targets=intent,
                reference_prices={
                    symbol: float(bar["close"])
                    for symbol, bar in bars.items()
                    if bar.get("close") is not None
                },
                reference_volumes={
                    symbol: (float(bar["volume"]) if bar.get("volume") is not None else None)
                    for symbol, bar in bars.items()
                },
                lagged_adv_by_symbol=dict(lagged_adv_by_symbol or {}),
                decided_at=ts,
            )
            self._persist_state()
            return self._continue_live_rebalance(
                bars,
                ts,
                lagged_adv_by_symbol=lagged_adv_by_symbol,
            )

        try:
            requests = self._plan_live_orders(
                intent,
                bars,
                ts,
                apply_volume_limit=apply_volume_limit,
                lagged_adv_by_symbol=lagged_adv_by_symbol,
            )
        except ValueError as exc:
            self._halt_live(title="Live Order Preflight Rejected", message=str(exc))
            return False

        if not requests:
            return True
        if self._active_orders:
            raise RuntimeError("cannot enqueue a new intent while broker orders are active")

        queued = [TrackedOrder(request=request) for request in requests]
        self._active_orders.extend(queued)
        # Marked before the write, so legs a failed write leaves queued are still
        # judged. Assigned, not added: the queue was empty, so any older id is stale.
        self._unstarted_groups = {
            request.group_id for request in requests if request.group_id is not None
        }
        self._persist_state(*queued)
        self._advance_live_orders()
        return not self._active_orders and not self._halted

    def _queue_next_live_rebalance_order(
        self,
        bars: dict[str, dict[str, float]],
        ts: datetime,
        *,
        lagged_adv_by_symbol: dict[str, float] | None = None,
    ) -> bool:
        """Recalculate one target leg from a coherent completed-bar snapshot."""
        batch = self._live_rebalance
        if batch is None:
            return False
        if self._active_orders:
            raise RuntimeError("cannot replan live targets while an order is active")

        if batch.execution_bar_ts != ts:
            batch.filled_bar_quantity_by_symbol.clear()
        batch.execution_bar_ts = ts
        batch.reference_prices = {
            symbol: float(bar["close"])
            for symbol, bar in bars.items()
            if bar.get("close") is not None and float(bar["close"]) > 0
        }
        batch.reference_volumes = {
            symbol: (float(bar["volume"]) if bar.get("volume") is not None else None)
            for symbol, bar in bars.items()
        }
        batch.lagged_adv_by_symbol = dict(lagged_adv_by_symbol or {})
        # While paused, only the reductions are planned: one the volume limit
        # caps defers to the next bar as it does unpaused, and the entries,
        # never sent, hold nothing up. With no reduction left the rebalance
        # ends, recording the entries it withheld.
        withheld: set[str] | None = set() if self._entries_paused else None
        required_symbols = set(self._positions)
        if withheld is None:
            required_symbols |= {
                symbol for symbol, weight in batch.targets.weights.items() if abs(weight) > EPSILON
            }
        try:
            missing = sorted(required_symbols - set(batch.reference_prices))
            if missing:
                raise ExecutionPriceUnavailableError(missing)
            requests = self._plan_live_orders(
                batch.targets,
                bars,
                ts,
                lagged_adv_by_symbol=lagged_adv_by_symbol,
                used_bar_quantity_by_symbol=batch.filled_bar_quantity_by_symbol,
                sequence_start=batch.next_sequence,
                withheld_entries=withheld,
            )
        except ExecutionUnavailableError as exc:
            if batch.delay_bars >= self._max_rebalance_delay_bars:
                self._halt_live(
                    title="Live Rebalance Delay Exceeded",
                    message=(
                        "PortfolioWeights exceeded "
                        f"max_rebalance_delay_bars={self._max_rebalance_delay_bars} "
                        f"for {list(exc.symbols)} at {ts}: {exc}"
                    ),
                )
                return False
            batch.delay_bars += 1
            logger.info(
                "Deferring live PortfolioWeights at %s (%d/%d bars): %s",
                ts,
                batch.delay_bars,
                self._max_rebalance_delay_bars,
                exc,
            )
            if self._on_runtime_event:
                self._on_runtime_event(
                    RuntimeEvent(
                        ts=ts,
                        event_type="decision_skipped",
                        detail={
                            "reason": "rebalance_deferred",
                            "symbols": list(exc.symbols),
                            "delay_bars": batch.delay_bars,
                            "max_rebalance_delay_bars": self._max_rebalance_delay_bars,
                            "message": str(exc),
                        },
                    )
                )
            self._persist_state()
            return False
        except ValueError as exc:
            self._halt_live(title="Live Rebalance Replan Rejected", message=str(exc))
            return False
        if not requests:
            self._live_rebalance = None
            self._persist_state()
            if withheld:
                symbols = sorted(withheld)
                message = (
                    f"target rebalance ended with its reductions done; {', '.join(symbols)} "
                    "not opened or added while entries are paused"
                )
                logger.info("Rebalance entries withheld: %s", message)
                if self._on_runtime_event:
                    self._on_runtime_event(
                        RuntimeEvent(
                            ts=ts,
                            event_type="decision_skipped",
                            detail={
                                "reason": "entries_paused",
                                "symbols": symbols,
                                "message": message,
                            },
                        )
                    )
            return False

        tracked = TrackedOrder(request=requests[0])
        batch.next_sequence += 1
        self._active_orders.append(tracked)
        self._persist_state(tracked)
        return True

    def _continue_live_rebalance(
        self,
        bars: dict[str, dict[str, float]],
        ts: datetime,
        *,
        lagged_adv_by_symbol: dict[str, float] | None = None,
    ) -> bool:
        """Advance serial target legs using only one current market snapshot.

        Returns whether the rebalance is complete. A leg that fails or is
        refused ends it short of its targets, and the trader remembers the
        bar, so that no decision on it replans them.
        """
        while self._live_rebalance is not None and not self._active_orders and not self._halted:
            if not self._queue_next_live_rebalance_order(
                bars,
                ts,
                lagged_adv_by_symbol=lagged_adv_by_symbol,
            ):
                break
            self._advance_live_orders()
            if self._live_rebalance is None:
                self._rebalance_stopped_at = ts
        return self._live_rebalance is None and not self._active_orders and not self._halted

    def _timed_order_call[T](self, callback: Callable[[], T]) -> T:
        started = perf_counter()
        try:
            return callback()
        finally:
            self._cycle_order_seconds += perf_counter() - started

    def _advance_live_orders(self, *, submit_planned: bool = True) -> None:
        """Poll or submit the head order; dependent orders stay serialized.

        A requested flatten does this work itself, reading, booking and
        cancelling every tracked order. Done here first, a timeout, a venue
        cancel or a fill past a risk limit would halt the account, which then
        refuses the flatten and leaves its positions open, and a queued order
        would go out ahead of it.
        """
        while self._active_orders and (
            not self._halted
            or self._has_active_recovery_orders()
            or self._active_orders[0].cancel_requested
            or self._active_orders[0].status == "cancel_pending"
        ):
            if submit_planned and self._flatten_cancels_this_cycle():
                return
            tracked = self._active_orders[0]
            request = tracked.request
            # The engine's own cancels finish here, as does any cancel of an
            # order a halt owns. One the venue started on its own is polled
            # below and fails the order when it ends, as any venue cancel does.
            if tracked.cancel_requested or (
                self._halted
                and tracked.status == "cancel_pending"
                and not _is_recovery_exit(request)
            ):
                try:
                    self._cancel_tracked_order(tracked)
                except ReportNotAppliedError as exc:
                    # The cancel went out regardless. A halt has stopped
                    # trading already. Otherwise what the venue holds is
                    # unknown, unless the cancel's own answer applied and
                    # ended the order: then it ends as any cancel does.
                    if self._halted:
                        self._alert_unapplied_while_halted(exc)
                    elif tracked in self._active_orders:
                        self._halt_live(title="Order Reconciliation Failed", message=str(exc))
                        return
                    else:
                        self._alert_unapplied(exc)
                if self._post_fill_risk_halted():
                    return
                if tracked in self._active_orders or tracked.status not in (
                    "cancelled",
                    "rejected",
                ):
                    return
                # On a halted account the cancel was the halt's or the exit's
                # own timeout's, which a restart leaves no way to tell apart;
                # a recovery exit's position stays open either way, so it is
                # recorded, and a strategy order's end was the halt's to name.
                if self._halted:
                    if _is_recovery_exit(request):
                        self._report_exit_a_halt_ended(tracked, in_halt_pass=False)
                        continue
                    return
                # Over age, the order fails as its timeout would have failed
                # it, whichever cancel this was: a restart between the
                # timeout's recorded cancel and its outcome leaves that to
                # this pass, and drops a flatten request. A younger order's
                # cancel was a flatten's or a failed group's, each alerting on
                # its own, as are the cancels of a flatten still waiting on them.
                if self._flatten_request is not None or not self._live_order_timed_out(tracked):
                    return
                if self._fail_group_or_halt(
                    tracked, title="Order Timeout", message=_timeout_message(tracked)
                ):
                    return
                continue
            polled = tracked.placement_attempted
            if not polled:
                if not submit_planned:
                    return
                if self._drop_paused_entry(tracked) or self._drop_unfunded_entry(tracked):
                    continue
                # Persist placement-attempted before network I/O. A crash in
                # the following call is recovered by client-order lookup and
                # never blindly retried.
                attempted_at = self._clock()
                if attempted_at.tzinfo is None:
                    raise ValueError("clock must return a timezone-aware datetime")
                tracked.placement_attempted = True
                tracked.placement_attempted_at = attempted_at.astimezone(UTC)
                # Its first leg starts a group, recorded in the same write.
                if request.group_id is not None:
                    self._unstarted_groups.discard(request.group_id)
                self._persist_state(tracked)
                try:
                    report = self._timed_order_call(
                        lambda request=request: self._executor.submit_order(request)
                    )
                except Exception as exc:
                    # The order may rest at the venue, so its status stays
                    # unresolved. The error lands with this order's next row
                    # write (the lookup's report, or the halt's cancellation),
                    # not a write of its own that could fail ahead of them.
                    tracked.error = f"{type(exc).__name__}: {exc}"
                    report = None
                if report is None:
                    report = self._timed_order_call(
                        lambda request=request: self._executor.find_order(request)
                    )
                    if report is None:
                        # Genuinely unknown broker state (not a confirmed
                        # rejection) — always halt the whole account, even
                        # for a grouped leg, rather than risk losing track
                        # of an order that may still be live at the venue.
                        self._halt_live(
                            title="Ambiguous Order Placement",
                            message=(
                                f"{request.symbol} qty={request.quantity:.4f} client_order_id="
                                f"{request.client_order_id} was not found after placement failure"
                            ),
                        )
                        return
            else:
                try:
                    if tracked.order_id:
                        report = self._timed_order_call(
                            lambda request=request, order_id=tracked.order_id: (
                                self._executor.get_order(request, order_id)
                            )
                        )
                    else:
                        report = self._timed_order_call(
                            lambda request=request: self._executor.find_order(request)
                        )
                except ValueError as exc:
                    self._poll_answer_not_applied(
                        tracked, tracked.order_id, exc, submit_planned=submit_planned
                    )
                    return
                except Exception as exc:
                    if not self._live_order_timed_out(tracked):
                        # The cycle fails and the next one reads again; a Poll
                        # Error that follows names the order.
                        raise RuntimeError(
                            f"{request.symbol} order_id={tracked.order_id or 'unassigned'} "
                            f"status unavailable ({type(exc).__name__}: {exc})"
                        ) from exc
                    logger.exception(
                        "Order status unavailable after local timeout: %s",
                        request.client_order_id,
                    )
                    self._cancel_timed_out_order(tracked, None, status_error=exc)
                    return
                if report is None:
                    # Same reasoning as "Ambiguous Order Placement" above:
                    # unknown broker state always halts, group or not.
                    self._halt_live(
                        title="Ambiguous Restored Order",
                        message=(
                            f"{request.symbol} client_order_id={request.client_order_id} "
                            "was placement-attempted but cannot be found"
                        ),
                    )
                    return

            try:
                self._apply_order_report(tracked, report)
            except ValueError as exc:
                # A placement's answer still raises, before anything changed,
                # and the next poll retries it here.
                if not polled:
                    raise
                self._poll_answer_not_applied(
                    tracked,
                    tracked.order_id or report.order_id,
                    exc,
                    submit_planned=submit_planned,
                )
                return
            if self._post_fill_risk_halted():
                return
            if report.status == "cancel_pending":
                # The venue is cancelling it on its own; its outcome is polled,
                # and one that reverts to working is under its timeout again.
                return
            if report.status in ("cancelled", "rejected"):
                message = (
                    f"{request.symbol} order_id={report.order_id or 'unassigned'} "
                    f"filled={report.filled_quantity:.4f}/"
                    f"{report.requested_quantity:.4f}"
                )
                if report.rejection_reason:
                    message += f"; venue: {report.rejection_reason}"
                if report.account_fault:
                    message += "; the venue refused the account, not only this order"
                if self._fail_group_or_halt(
                    tracked,
                    title=f"Order {report.status.title()}",
                    message=message,
                    account_fault=report.account_fault,
                ):
                    return
                continue
            if report.status != "filled" and self._live_order_timed_out(tracked):
                self._cancel_timed_out_order(tracked, report)
                return
            if report.status != "filled":
                return

    def _poll_answer_not_applied(
        self, tracked: TrackedOrder, order_id: str, error: ValueError, *, submit_planned: bool
    ) -> None:
        """Alert a polled answer that does not apply to its order, whether read or applied.

        The order may still be working, so it stays tracked and under its
        timeout, and the rest of the cycle goes on. A restore at startup
        fails closed instead, naming the order, unless the order is past its
        timeout: then its cancel goes out, as on a running account.
        """
        self._note_unappliable(tracked)
        failure = _report_failure(tracked.request, order_id, error)
        timed_out = self._live_order_timed_out(tracked)
        if not submit_planned and not timed_out:
            raise ValueError(failure) from error
        self._alert_report_not_applied(
            tracked,
            f"{failure}; "
            + (
                "cancelling it at its timeout"
                if timed_out
                else "the order stays tracked and is polled again next cycle"
            ),
        )
        if timed_out:
            self._cancel_timed_out_order(tracked, None, status_error=error)

    def _drop_paused_entry(self, tracked: TrackedOrder) -> bool:
        """Drop an unsubmitted entry, with its group, while entries are paused.

        A group is judged once, before its first leg goes out, for the hedge
        reason ``_drop_unfunded_entry`` gives; a started group finishes. A
        target rebalance queues no entry while paused, so one dropped here was
        queued before the pause; the rebalance carries on, as replanning
        while paused plans only its reductions. A dropped exit stops it.
        Returns whether anything was dropped.
        """
        if not self._entries_paused:
            return False
        group_id = tracked.request.group_id
        if group_id is None:
            legs = [tracked]
        elif group_id in self._unstarted_groups:
            legs = [order for order in self._active_orders if order.request.group_id == group_id]
        else:
            return False
        if all(self._reduces_held_position(leg.request) for leg in legs):
            return False
        scope = "other orders remain eligible"
        if self._live_rebalance is not None and any(
            leg.request.position_effect in ("reduce", "close") for leg in legs
        ):
            # Replanning would queue the same exit again.
            self._live_rebalance = None
            scope = "the target rebalance stopped"
        if group_id is not None:
            self._unstarted_groups.discard(group_id)
            scope = f"group {group_id!r} dropped whole"
        # Out of the queue before the one write, so a failed write leaves no
        # leg to go out.
        for leg in legs:
            leg.status = "cancelled"
            self._active_orders.remove(leg)
        self._persist_state(*legs)
        for leg in legs:
            request = leg.request
            message = (
                f"{request.side} {request.quantity:g} to {request.position_effect} "
                f"not submitted while entries are paused; {scope}"
            )
            logger.info("Dropped %s order: %s", request.symbol, message)
            if self._on_runtime_event:
                self._on_runtime_event(
                    RuntimeEvent(
                        ts=self._utc_now(),
                        event_type="decision_skipped",
                        symbol=request.symbol,
                        detail={
                            "reason": "entries_paused",
                            "group_id": group_id,
                            "position_effect": request.position_effect,
                            "quantity": request.quantity,
                            "message": message,
                        },
                    )
                )
        return True

    def _reduces_held_position(self, request: OrderRequest) -> bool:
        """Whether ``request`` only reduces the ledger position it trades against.

        Its position effect alone is not enough: an exit is sized at planning
        against a staged book that may include an entry dropped since, and
        sent as sized it would cross the position.
        """
        position = self._positions.get(request.symbol)
        return (
            request.position_effect in ("reduce", "close")
            and position is not None
            and request.side == ("sell" if position.side == "long" else "buy")
            and request.quantity <= position.quantity + EPSILON
        )

    def _drop_unfunded_entry(self, tracked: TrackedOrder) -> bool:
        """Drop an unsubmitted entry the broker's free balance cannot fund.

        The ledger sizes every order and is never overwritten from the broker,
        so cash that left the account (a withdrawal, a loss booked outside the
        ledger, another strategy on the account) would still size entries, and
        a margin venue may accept such an order at a higher effective leverage
        than the strategy sized for. The requirement is the outlay the ledger
        debits for the entry, margin plus costs. An unreadable balance drops
        the entry too. Exits are never checked on their own, and the orders
        queued after a dropped entry still go out.

        A group is checked once, at its first leg, since its legs go out
        serially and withholding a later leg would leave the earlier ones
        unhedged. A refused group is cancelled whole before any leg is sent.
        Returns whether anything was dropped.
        """
        request = tracked.request
        group_id = request.group_id
        if group_id is None:
            if request.position_effect not in ("open", "add"):
                return False
            legs = [tracked]
            subject = request.symbol
        else:
            if group_id not in self._unstarted_groups:
                return False
            legs = [order for order in self._active_orders if order.request.group_id == group_id]
            if all(leg.request.position_effect in ("reduce", "close") for leg in legs):
                return False
            subject = f"group {group_id!r}"
        adapter = self._executor.get_order_adapter(request.symbol)
        if not callable(getattr(adapter, "get_balance", None)):
            logger.warning(
                "Free balance check skipped for the %s entry: order adapter has no "
                "get_balance capability",
                subject,
            )
            return False
        entry_symbols = {
            leg.request.symbol for leg in legs if leg.request.position_effect in ("open", "add")
        }
        currency = self._currency
        required: float | None = None
        free: float | None = None
        error: Exception | None = None
        try:
            required = self._cash_needed_up_front(legs)
        except Exception as exc:
            error = exc
        else:
            try:
                balance = self._timed_order_call(lambda: adapter.get_balance(currency))
                free = float(balance["free"])
                if not isfinite(free):
                    raise ValueError(f"free balance is not finite: {free}")
            except Exception as exc:
                free, error = None, exc
            else:
                if free + EPSILON >= required:
                    self._unfunded_entry_alerts = {
                        key for key in self._unfunded_entry_alerts if key[1] not in entry_symbols
                    }
                    return False

        scope = "other orders remain eligible"
        if self._live_rebalance is not None:
            # Replanning would queue the same leg again; the next bar's
            # decision replans the targets from the current book.
            self._live_rebalance = None
            scope = "the target rebalance stopped"
        if group_id is not None:
            scope = f"group {group_id!r} cancelled, other groups unaffected"
        for leg in legs:
            self._cancel_tracked_order(leg)
        if group_id is not None:
            # Only now: a write that failed above leaves the rest queued to be judged again.
            self._unstarted_groups.discard(group_id)
        if group_id is None:
            what = f"{request.side} {request.quantity:g} to {request.position_effect}"
            needs = "needs"
        else:
            what, needs = f"{len(legs)} legs", "need"
        failure = f"{type(error).__name__}: {error}"
        if required is None:
            reason, title = "free_balance_unavailable", "Unpriced Entry"
            outcome = f"{what} could not be priced ({failure})"
        elif error is not None:
            reason, title = "free_balance_unavailable", "Free Balance Unavailable"
            outcome = (
                f"{what} {needs} {required:.2f} {currency} but the broker free balance "
                f"could not be read ({failure})"
            )
        else:
            reason, title = "insufficient_free_balance", "Insufficient Free Balance"
            outcome = (
                f"{what} {needs} {required:.2f} {currency} but the broker free balance "
                f"is {free:.2f} {currency}"
            )
        message = f"{outcome}; not submitted, {scope}."
        logger.warning("%s for %s: %s", title, subject, message, exc_info=error)
        if self._on_runtime_event:
            self._on_runtime_event(
                RuntimeEvent(
                    ts=self._utc_now(),
                    event_type="decision_skipped",
                    symbol=request.symbol,
                    detail={
                        "reason": reason,
                        "group_id": group_id,
                        "quantity": request.quantity if group_id is None else None,
                        "required": required,
                        "free": free,
                        "message": message,
                    },
                )
            )
        # Keyed by symbol rather than group id, which may be new every decision.
        alert_keys = {(title, symbol) for symbol in entry_symbols}
        if not alert_keys <= self._unfunded_entry_alerts:
            self._unfunded_entry_alerts |= alert_keys
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] {title}: {subject}",
                message=message,
            )
        return True

    def _cash_needed_up_front(self, legs: list[TrackedOrder]) -> float:
        """Cash *legs* need from the account when their first entry goes out.

        Only exits queued before that entry are credited: one queued after it
        has released nothing yet when the entry is placed.
        """
        need = 0.0
        entry_reached = False
        for leg in legs:
            is_entry = leg.request.position_effect in ("open", "add")
            entry_reached = entry_reached or is_entry
            if is_entry or not entry_reached:
                need += self._cash_needed_by(leg.request)
        return need

    def _cash_needed_by(self, request: OrderRequest) -> float:
        """Cash an order takes from the account, negative for what an exit releases."""
        price = (
            request.limit_price
            if request.limit_price is not None
            else self._last_prices[request.symbol]
        )
        cost_model = self._get_cost_model(request.symbol)
        if request.position_effect in ("open", "add"):
            side: Literal["long", "short"] = "long" if request.side == "buy" else "short"
            return cost_model.estimate_entry_outlay(price, request.quantity, side)
        position = self._positions.get(request.symbol)
        if position is None:
            return 0.0
        _, proceeds, _ = close_position(position, price, cost_model, quantity=request.quantity)
        return -proceeds

    def _live_order_timed_out(self, tracked: TrackedOrder) -> bool:
        """Return whether a placement-attempted order exceeded its local timeout."""
        timeout_seconds = self._live_order_timeout_seconds
        if timeout_seconds is None:
            return False
        attempted_at = tracked.placement_attempted_at
        if attempted_at is None:
            raise RuntimeError("placement-attempted order is missing placement_attempted_at")
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return (now.astimezone(UTC) - attempted_at).total_seconds() >= timeout_seconds

    def _cancel_timed_out_order(
        self,
        tracked: TrackedOrder,
        latest_report: ExecutionReport | None,
        *,
        status_error: Exception | None = None,
    ) -> None:
        """Cancel one stale live order, preserving any cumulative broker fill."""
        request = tracked.request
        order_id = (latest_report.order_id if latest_report is not None else "") or tracked.order_id
        if not order_id:
            # No order id to even attempt a cancel with — unknown broker
            # state, always halt regardless of group_id (see
            # _fail_group_or_halt's docstring).
            self._halt_live(
                title="Order Timeout Unresolved",
                message=(
                    f"{request.symbol} client_order_id={request.client_order_id} "
                    "exceeded its local timeout without a broker order id"
                    + (f"; latest status error: {status_error}" if status_error else "")
                ),
            )
            return
        # Checkpoint cancellation intent before broker I/O. A restart can
        # retry this transition by order id without resubmitting placement.
        tracked.cancel_requested = True
        self._persist_state(tracked)
        try:
            cancel_report = self._send_cancel(tracked, order_id)
            self._apply_order_report(tracked, cancel_report)
        except Exception as exc:
            if isinstance(exc, ValueError):
                # The venue answered, with something that does not apply.
                self._note_unappliable(tracked)
            # The cancel attempt itself failed — still unknown broker state.
            self._halt_live(
                title="Order Timeout Cancellation Failed",
                message=(
                    f"{request.symbol} order_id={order_id} could not be confirmed cancelled: {exc}"
                    + (f"; latest status error: {status_error}" if status_error else "")
                ),
            )
            return
        if self._post_fill_risk_halted():
            return

        if cancel_report.status == "filled":
            return
        status = cancel_report.status
        message = _timeout_message(tracked)
        if status in ("cancelled", "rejected"):
            self._fail_group_or_halt(tracked, title="Order Timeout", message=message)
        else:
            # Cancellation itself didn't reach a confirmed terminal status —
            # still unknown broker state, always halt.
            tracked.status = cancel_report.status
            self._persist_state(tracked)
            self._halt_live(title="Order Timeout Cancellation Unresolved", message=message)

    def _note_unappliable(self, tracked: TrackedOrder) -> None:
        """Record an answer met that does not apply, whether or not it is alerted.

        Some are only logged or folded into a halt's alert, and still must
        not let an applied answer in the same pass, or an earlier one, read
        as the problem gone.
        """
        client_order_id = tracked.request.client_order_id
        self._report_failed_pass[client_order_id] = self._order_pass
        self._report_alerts.hold(client_order_id)

    def _apply_order_report(
        self,
        tracked: TrackedOrder,
        report: ExecutionReport,
    ) -> None:
        """Apply only the new cumulative-fill delta, then checkpoint it."""
        request = tracked.request
        if (
            report.client_order_id != request.client_order_id
            or report.symbol != request.symbol
            or report.side != request.side
        ):
            raise ValueError("broker report identity does not match the tracked request")
        if tracked.order_id and report.order_id != tracked.order_id:
            raise ValueError("broker order id changed during its lifecycle")
        if report.filled_quantity + EPSILON < tracked.filled_quantity:
            raise ValueError("broker cumulative filled quantity moved backwards")
        if abs(report.requested_quantity - request.quantity) > EPSILON:
            raise ValueError("broker requested quantity does not match the tracked request")

        result: ExecutionResult | None = None
        cumulative_notional = (
            report.filled_quantity * report.average_price
            if report.average_price is not None
            else 0.0
        )
        delta_quantity = report.filled_quantity - tracked.filled_quantity
        if delta_quantity > EPSILON:
            if report.executed_at is None:
                raise ValueError("new broker fill is missing execution time")
            delta_notional = cumulative_notional - tracked.filled_notional
            if delta_notional <= 0:
                raise ValueError("broker cumulative fill notional moved backwards")
            delta_tax = report.tax - tracked.tax
            if delta_tax < -EPSILON:
                raise ValueError("broker cumulative execution costs moved backwards")
            adv_session = self._fill_adv_session(report.symbol, report.executed_at)
            fill = Fill(
                symbol=report.symbol,
                side="long" if report.side == "buy" else "short",
                price=delta_notional / delta_quantity,
                quantity=delta_quantity,
                commission=report.commission - tracked.commission,
                # WHY: the broker price already embeds slippage; it is
                # measured as the order's implementation shortfall instead.
                slippage=0.0,
                tax=max(0.0, delta_tax),
            )
            self._cash, result = apply_execution_fill(
                self._positions,
                self._cash,
                fill,
                report.executed_at,
                order_side=report.side,
                cost_model=self._get_cost_model(report.symbol),
                reason=request.reason,
                group_id=request.group_id,
                time_in_force=request.time_in_force,
            )
            self._record_adv_fill(report.symbol, delta_quantity, adv_session)
            # A halted account is not checked, and a mark left from its cancel
            # passes would halt the first exit after a reset.
            if request.position_effect in ("open", "add") and not self._halted:
                self._entry_fill_unchecked = tracked
            if self._live_rebalance is not None:
                consumed = self._live_rebalance.filled_bar_quantity_by_symbol
                consumed[report.symbol] = consumed.get(report.symbol, 0.0) + delta_quantity

        tracked.order_id = report.order_id or tracked.order_id
        tracked.status = report.status
        tracked.filled_quantity = report.filled_quantity
        tracked.filled_notional = cumulative_notional
        tracked.commission = report.commission
        tracked.tax = report.tax
        tracked.executed_at = report.executed_at or tracked.executed_at
        tracked.error = report.rejection_reason or tracked.error
        if report.status in ("filled", "cancelled", "rejected"):
            self._active_orders.remove(tracked)
            self._cancels_sent.pop(request.client_order_id, None)
        if result is not None:
            self._trade_count += len(result.trades)
            self._unpublished_results.append(result)
        try:
            self._persist_state(tracked)
            self._publish_checkpointed_results()
        except ValueError as exc:
            # Booked already: every caller reads a ValueError as an answer
            # that does not apply, and would report this one as not applied.
            raise RuntimeError(f"applied broker report not recorded: {exc}") from exc
        # Last, so no alert runs between booking a fill and recording it. An
        # answer that applies in the pass where another failed has not shown
        # the failure gone, such as a cancel's after an unappliable read.
        if self._report_failed_pass.get(request.client_order_id) != self._order_pass:
            self._report_alerts.clear(request.client_order_id)

    def _cancel_tracked_order(self, tracked: TrackedOrder) -> None:
        """Reconcile and cancel one tracked order without resubmitting it.

        The order stays tracked while it may still be working. The cancel goes
        out unless one this process sent still stands
        (``_cancel_outcome_so_far``): neither a pending state the venue set
        itself nor a failed status read holds it back, and in a later pass
        one the venue turned down, leaving the order working, is sent again,
        as is one whose pending state this pass could not read.
        NautilusTrader and LEAN likewise hold back a second cancel only while
        their own is pending.

        A ValueError from a broker call or from applying its report is a venue
        answer that does not reconcile with the tracked order, raised by
        ``_cancel_unreconciled`` once the cancel is out; any other broker error
        is the venue not answering, logged, and the cancel goes out by order id
        all the same.
        """
        if not tracked.placement_attempted:
            tracked.status = "cancelled"
            self._active_orders.remove(tracked)
            self._persist_state(tracked)
            return

        if not tracked.cancel_requested:
            tracked.cancel_requested = True
            self._persist_state(tracked)

        request = tracked.request
        order_id = tracked.order_id
        cancel_sent = False
        status_read = False
        try:
            try:
                report = self._timed_order_call(
                    lambda: (
                        self._executor.get_order(request, order_id)
                        if order_id
                        else self._executor.find_order(request)
                    )
                )
            except ValueError:
                raise
            except Exception:
                logger.exception(
                    "Failed to reconcile tracked order before cancellation %s",
                    request.client_order_id,
                )
                if not order_id:
                    return
            else:
                if report is None:
                    logger.error("Cannot cancel unresolved order %s", request.client_order_id)
                    return
                order_id = order_id or report.order_id
                self._apply_order_report(tracked, report)
                if report.status in ("filled", "cancelled", "rejected"):
                    return
                status_read = True
            if self._cancel_outcome_so_far(tracked, status_read=status_read) is not None:
                return

            cancel_sent = True
            try:
                cancel_report = self._send_cancel(tracked, order_id)
            except ValueError:
                raise
            except Exception:
                logger.exception("Failed to cancel tracked order %s", request.client_order_id)
                return
            self._apply_order_report(tracked, cancel_report)
        except ValueError as exc:
            self._cancel_unreconciled(tracked, order_id, exc, cancel_sent=cancel_sent)
            return

    def _cancel_outcome_so_far(self, tracked: TrackedOrder, *, status_read: bool) -> str | None:
        """What became of a cancel this process sent that still stands, or None if none does.

        One stands through the rest of the pass that sent it, and while the
        venue reports it pending in an answer ``status_read`` says this pass
        applied: a pending state from an earlier pass, with the status now
        unreadable, may hide an order the venue put back to work.
        """
        sent_in = self._cancels_sent.get(tracked.request.client_order_id)
        if sent_in is None:
            return None
        if status_read and tracked.status == "cancel_pending":
            return "cancel already pending"
        return "cancel already sent" if sent_in == self._order_pass else None

    def _send_cancel(self, tracked: TrackedOrder, order_id: str) -> ExecutionReport:
        """Cancel ``tracked`` by order id, recording the cancel once the venue answers.

        A ValueError is an answer too. The caller applies the report.
        """
        request = tracked.request
        try:
            report = self._timed_order_call(lambda: self._executor.cancel_order(request, order_id))
        except ValueError:
            self._cancels_sent[request.client_order_id] = self._order_pass
            raise
        if report is None:
            raise RuntimeError("broker cancellation returned no execution report")
        self._cancels_sent[request.client_order_id] = self._order_pass
        return report

    def _cancel_unreconciled(
        self, tracked: TrackedOrder, order_id: str, error: ValueError, *, cancel_sent: bool
    ) -> None:
        """Send the cancel a venue answer that does not reconcile held back, then raise it.

        The order may still be working at the venue, and the answer may carry
        fills the ledger lacks, so the error names the order and what became
        of its cancel. The order stays tracked until a report that applies
        ends it. A cancel this process sent that still stands is not sent again.
        """
        # Before the cancel below, whose answer may apply.
        self._note_unappliable(tracked)
        request = tracked.request
        failure = _report_failure(request, order_id, error)
        standing = self._cancel_outcome_so_far(tracked, status_read=False)
        if cancel_sent:
            outcome = "cancel sent"
        elif standing is not None:
            outcome = standing
        elif not order_id:
            outcome = "no order id to cancel"
        else:
            try:
                # An error here may come before anything reached the venue.
                cancel_report = self._send_cancel(tracked, order_id)
            except Exception as exc:
                logger.exception("Cancel not confirmed for %s", request.client_order_id)
                outcome = f"cancel not confirmed ({type(exc).__name__}: {exc})"
            else:
                try:
                    self._apply_order_report(tracked, cancel_report)
                except ValueError as exc:
                    outcome = f"cancel sent, its answer not applied ({type(exc).__name__}: {exc})"
                except Exception as exc:
                    logger.exception("Cancel answer not recorded for %s", request.client_order_id)
                    outcome = f"cancel sent, its answer not recorded ({type(exc).__name__}: {exc})"
                else:
                    outcome = "cancel sent"
        raise ReportNotAppliedError(f"{failure}; {outcome}", tracked, content=failure) from error

    def _cancel_active_orders(self) -> None:
        """Cancel every tracked order, then raise the first failure, if any.

        A failure at one order does not stop the others, so an order queued
        behind it cannot go out ahead of a flatten that waits on this pass.
        On a halted account, each recovery exit the pass ends short of a fill
        is recorded once the pass is done, so a failing record cannot hold
        back a cancel.
        """
        failure: Exception | None = None
        ended_exits: list[TrackedOrder] = []
        for tracked in list(self._active_orders):
            try:
                self._cancel_tracked_order(tracked)
            except Exception as exc:
                if failure is None:
                    failure = exc
                else:
                    logger.error(
                        "Cancelling %s also failed",
                        tracked.request.client_order_id,
                        exc_info=exc,
                    )
            if (
                self._halted
                and _is_recovery_exit(tracked.request)
                and tracked not in self._active_orders
                and tracked.status in ("cancelled", "rejected")
            ):
                ended_exits.append(tracked)
        for tracked in ended_exits:
            try:
                self._report_exit_a_halt_ended(tracked, in_halt_pass=True)
            except Exception:
                logger.exception(
                    "Skipped close not recorded for %s", tracked.request.client_order_id
                )
        if failure is not None:
            raise failure

    def _report_exit_a_halt_ended(self, tracked: TrackedOrder, *, in_halt_pass: bool) -> None:
        """Record a recovery exit that ended short of a fill on a halted account.

        Its position stays open, and the halt's own alert names only the
        orders it has not yet seen end. Which cancel ended it, a halt's or
        the exit's timeout's, or whether the venue ended it first, is not
        known, so the cause says only when it ended: ``in_halt_pass`` is
        whether a halt's cancel pass, which goes to every order, saw it end.
        """
        request = tracked.request
        position = self._positions.get(request.symbol)
        ended = f"ended {tracked.status}" if tracked.placement_attempted else "dropped unsent"
        if in_halt_pass:
            cause = f"{ended} while a halt cancelled the account's orders"
            others = "the halt cancels the account's other orders too"
        else:
            cause = f"{ended} after a cancel the engine requested"
            others = f"still tracked: {self._tracked_orders_summary() or 'none'}"
        self._report_skipped_close(
            request.symbol,
            quantity=request.quantity - tracked.filled_quantity,
            held=position.quantity if position is not None else None,
            ts=self._utc_now(),
            reason=f"close_{tracked.status}",
            title=f"Close {tracked.status.title()}",
            cause=cause,
            others=others,
        )

    def _process_bar(self, symbol: str, raw_df: pd.DataFrame, ts: datetime) -> None:
        """Process one symbol through the portfolio-cycle path."""
        self._process_cycle({symbol: raw_df}, ts)

    def _build_feature_batch(
        self,
        raw_frames: Mapping[str, pd.DataFrame],
        active_symbols: tuple[str, ...],
        ts: datetime,
    ) -> FeatureBatch:
        """Build one causal primary snapshot without using poll wall time as its frontier."""
        normalized_frames: dict[str, pd.DataFrame] = {}
        cutoffs: dict[str, datetime] = {}
        active_set = set(active_symbols)
        for symbol in self._symbols:
            raw_frame = raw_frames.get(symbol)
            if raw_frame is None:
                raw_frame = self._ohlcv_cache.get(symbol)
            if raw_frame is None:
                # An optional symbol may never have delivered. Whether the run
                # may proceed is the readiness gate's decision, already made
                # above; here it is simply a symbol with no visible history.
                raw_frame = pd.DataFrame(columns=["ts", AVAILABLE_AT_COLUMN])
            normalized = self._normalize_runtime_rows(symbol, raw_frame)
            normalized_frames[symbol] = normalized
            cutoff = ts if symbol in active_set else self._last_bar_ts.get(symbol)
            if cutoff is not None:
                cutoffs[symbol] = cutoff

        cohort_availability: list[pd.Timestamp] = []
        for symbol in active_symbols:
            frame = normalized_frames[symbol]
            current = frame.loc[pd.to_datetime(frame["ts"], utc=True) == pd.Timestamp(ts)]
            if current.empty:
                raise ValueError(f"{symbol} batch cohort is missing event timestamp {ts}")
            cohort_availability.extend(
                pd.to_datetime(current[AVAILABLE_AT_COLUMN], utc=True).tolist()
            )
        if not cohort_availability:
            raise ValueError("batch feature cohort has no causal availability frontier")
        as_of = max(cohort_availability)
        if self._last_feature_as_of is not None:
            as_of = max(as_of, pd.Timestamp(self._last_feature_as_of))

        histories = {}
        for subscription, owner in self._auxiliary_subscriptions.items():
            frame = self._auxiliary_cache.get(subscription)
            cutoff = cutoffs.get(owner)
            if frame is None or frame.empty or cutoff is None:
                # Registered with nothing visible rather than omitted: a
                # strategy that declared this identity must not get a KeyError
                # from ctx.market_data because one feed is down.
                frame = pd.DataFrame(columns=["ts", AVAILABLE_AT_COLUMN])
                cutoff = None
            timestamps = pd.to_datetime(frame["ts"], utc=True)
            available_at = pd.to_datetime(frame[AVAILABLE_AT_COLUMN], utc=True)
            visible = (
                frame.iloc[0:0]
                if cutoff is None
                else frame.loc[(timestamps <= pd.Timestamp(cutoff)) & (available_at <= as_of)].iloc[
                    -self._feature_history_limit :
                ]
            )
            auxiliary_history = visible.set_index("ts")
            auxiliary_history.index.name = "ts"
            histories[subscription] = auxiliary_history
        for symbol in self._symbols:
            frame = normalized_frames[symbol]
            cutoff = cutoffs.get(symbol)
            if cutoff is None:
                causal = frame.iloc[0:0]
            else:
                timestamps = pd.to_datetime(frame["ts"], utc=True)
                available_at = pd.to_datetime(frame[AVAILABLE_AT_COLUMN], utc=True)
                causal = frame.loc[
                    (timestamps <= pd.Timestamp(cutoff)) & (available_at <= as_of)
                ].iloc[-self._feature_history_limit :]
            history = causal.set_index("ts")
            history.index.name = "ts"
            histories[self._market_data_subscriptions[symbol]] = history

        market_data = MarketDataView._from_causal_frames(
            histories,
            as_of=as_of.to_pydatetime(),
            history_limit=self._feature_history_limit,
        )
        active_subscriptions = tuple(
            self._market_data_subscriptions[symbol]
            for symbol in self._symbols
            if symbol in active_set
        )
        return FeatureBatch(
            event_ts=ts,
            as_of=as_of.to_pydatetime(),
            primary_subscriptions=self._primary_subscriptions,
            active_primary_subscriptions=active_subscriptions,
            market_data=market_data,
        )

    def _adv_session(self, symbol: str, ts: datetime) -> str:
        """Session key one symbol's ADV budget is counted under at a bar."""
        if self._interval_delta.days >= 1:
            return pd.Timestamp(ts).date().isoformat()
        calendar_id = self._market_data_subscriptions[symbol].calendar_id
        return session_label(ts, calendar_id).isoformat()

    def _reset_adv_session(self, symbol: str, session: str) -> None:
        """Reset one symbol's cumulative ADV usage when its session changes."""
        if self._adv_session_labels.get(symbol) != session:
            self._adv_session_labels[symbol] = session
            self._adv_filled_quantities[symbol] = 0.0

    def _fill_adv_session(self, symbol: str, executed_at: datetime) -> str | None:
        """Session a confirmed fill counts toward; None keeps the current one.

        Resolved before the fill is applied, so a calendar that cannot place
        the execution fails before positions or cash change.
        """
        if self._adv_lookback_sessions is None:
            return None
        if self._interval_delta.days >= 1:
            if symbol in self._adv_session_labels:
                return None
            return pd.Timestamp(executed_at).date().isoformat()
        calendar_id = self._market_data_subscriptions[symbol].calendar_id
        return execution_session_label(executed_at, calendar_id).isoformat()

    def _record_adv_fill(self, symbol: str, quantity: float, session: str | None) -> None:
        """Accumulate a confirmed broker fill against its session budget."""
        if self._adv_lookback_sessions is None:
            return
        if session is not None:
            self._reset_adv_session(symbol, session)
        self._adv_filled_quantities[symbol] = (
            self._adv_filled_quantities.get(symbol, 0.0) + quantity
        )

    def _process_cycle(
        self,
        raw_frames: dict[str, pd.DataFrame],
        ts: datetime,
        *,
        active_symbols: list[str] | tuple[str, ...] | None = None,
    ) -> datetime | None:
        """Execute and evaluate one data-driven market event.

        Simulation fills previous-cycle intent on this completed raw bar.
        Live mode never books that historical range: it evaluates the
        completed bar, submits the current decision immediately, and commits
        only broker-confirmed execution reports.
        """
        primary_symbol = self._symbols[0]
        active_set = set(raw_frames) if active_symbols is None else set(active_symbols)
        resolved_active_symbols = tuple(symbol for symbol in self._symbols if symbol in active_set)
        if not resolved_active_symbols:
            raise ValueError("market-data event requires at least one active primary symbol")
        execution_symbols: set[str] = set()
        for symbol in resolved_active_symbols:
            execution_watermark = self._last_execution_bar_ts.get(symbol)
            if execution_watermark is not None and ts < execution_watermark:
                raise RuntimeError(
                    "out-of-order execution phase cannot be applied after a newer event: "
                    f"{symbol} {ts} < {execution_watermark}"
                )
            if execution_watermark != ts:
                execution_symbols.add(symbol)
        execution_already_committed = not execution_symbols
        cycle_used_bar_quantity_by_symbol = {
            symbol: self._execution_bar_filled_quantities.get(symbol, 0.0)
            for symbol in resolved_active_symbols
            if self._last_execution_bar_ts.get(symbol) == ts
        }
        feature_batch: FeatureBatch | None = None
        histories: dict[str, pd.DataFrame] = {}
        raw_bars: dict[str, dict[str, float]] = {}
        audit_bars: dict[str, dict[str, object]] = {}
        audit_histories: dict[str, pd.DataFrame] = {}
        previous_volumes: dict[str, float] = {}
        lagged_adv_by_symbol: dict[str, float] = {}
        for symbol in resolved_active_symbols:
            raw_df = raw_frames[symbol]
            audit_history = raw_df[raw_df["ts"] <= ts].iloc[-self._warmup_periods :].set_index("ts")
            audit_history.index.name = "ts"
            if audit_history.empty or pd.Timestamp(audit_history.index[-1]).to_pydatetime() != ts:
                continue
            history = audit_history.drop(columns=[AVAILABLE_AT_COLUMN], errors="ignore")
            histories[symbol] = history
            raw_bar = history.iloc[-1].to_dict()
            audit_bar = audit_history.iloc[-1].to_dict()
            audit_bar[OBSERVED_AT_COLUMN] = self._observed_at(
                symbol, ts, audit_bar.get(AVAILABLE_AT_COLUMN)
            )
            audit_bars[symbol] = audit_bar
            audit_histories[symbol] = audit_history
            close = float(raw_bar.get("close", float("nan")))
            if not isfinite(close) or close <= 0:
                raise ValueError(f"{symbol} has invalid close at {ts}: {close}")
            raw_bars[symbol] = raw_bar
            if len(history) > 1:
                previous_volume = history.iloc[-2].get("volume")
                if previous_volume is not None and not pd.isna(previous_volume):
                    previous_volumes[symbol] = float(previous_volume)
            self._last_prices[symbol] = close
            if self._adv_lookback_sessions is not None:
                self._reset_adv_session(symbol, self._adv_session(symbol, ts))
                calendar_id = self._market_data_subscriptions[symbol].calendar_id
                labels = None
                if self._interval_delta.days < 1:
                    labels = session_labels(
                        pd.DatetimeIndex(history.index),
                        calendar_id,
                    )
                lagged_adv = calculate_lagged_adv(
                    history["volume"],
                    self._adv_lookback_sessions,
                    session_labels=labels,
                ).iloc[-1]
                if not pd.isna(lagged_adv):
                    lagged_adv_by_symbol[symbol] = float(lagged_adv)

        if (
            not execution_already_committed
            and not self._executor.simulation
            and self._live_rebalance is not None
            and not self._active_orders
            and not self._halted
        ):
            self._continue_live_rebalance(
                raw_bars,
                ts,
                lagged_adv_by_symbol=lagged_adv_by_symbol,
            )

        live_rebalance_blocks_decisions = not self._executor.simulation and (
            self._live_rebalance is not None or bool(self._active_orders)
        )
        ready_decision: StrategyDecision = []
        if not execution_already_committed:
            self._pending_decision = self._without_halted_account(self._pending_decision)
            self._expire_resting_day_intents(ts, raw_bars, primary_symbol=primary_symbol)
            if not live_rebalance_blocks_decisions:
                ready_decision, waiting_decision = partition_pending_decision(
                    self._pending_decision,
                    raw_bars,
                    self._positions,
                    primary_symbol=primary_symbol,
                )
                self._pending_decision = waiting_decision
        if not execution_already_committed and self._executor.simulation:
            exposure_prices = dict(self._last_prices)
            exposure_prices.update(
                {
                    symbol: float(bar["open"])
                    for symbol, bar in raw_bars.items()
                    if bar.get("open") is not None
                }
            )
            if self._halted:
                step_result = check_stop_targets(
                    self._positions,
                    raw_bars,
                    ts,
                    get_cost_model=self._get_cost_model,
                    max_bar_volume_participation_rate=self._max_bar_volume_participation_rate,
                    max_adv_participation_rate=self._max_adv_participation_rate,
                    get_volume=lambda symbol: previous_volumes.get(symbol),
                    get_lagged_adv=lambda symbol: lagged_adv_by_symbol.get(symbol),
                    used_bar_quantity_by_symbol=cycle_used_bar_quantity_by_symbol,
                    used_adv_quantity_by_symbol=self._adv_filled_quantities,
                    eligible_symbols=execution_symbols,
                    get_executable_quantity=self._get_executable_quantity,
                )
                staged_cash = self._cash + step_result.cash_delta
            else:
                max_position_notional = None
                if self._risk_policy.max_position_weight:
                    execution_equity, _ = calc_equity(
                        self._cash,
                        self._positions,
                        get_price=lambda symbol, _position: exposure_prices[symbol],
                        get_cost_model=self._get_cost_model,
                    )
                    max_position_notional = self._risk_policy.max_position_weight * max(
                        execution_equity, 0.0
                    )
                staged_cash, step_result = execute_pending_decision_and_stops(
                    ts,
                    self._positions,
                    self._cash,
                    ready_decision,
                    raw_bars,
                    get_cost_model=self._get_cost_model,
                    default_fill=self._fill_price,
                    primary_symbol=primary_symbol,
                    max_position_notional=max_position_notional,
                    max_order_notional=self._risk_policy.max_order_notional,
                    max_bar_volume_participation_rate=self._max_bar_volume_participation_rate,
                    max_adv_participation_rate=self._max_adv_participation_rate,
                    get_previous_volume=lambda symbol: previous_volumes.get(symbol),
                    get_lagged_adv=lambda symbol: lagged_adv_by_symbol.get(symbol),
                    used_bar_quantity_by_symbol=cycle_used_bar_quantity_by_symbol,
                    used_adv_quantity_by_symbol=self._adv_filled_quantities,
                    eligible_stop_symbols=execution_symbols,
                    max_gross_exposure=self._risk_policy.max_gross_exposure,
                    max_net_exposure=self._risk_policy.max_net_exposure,
                    exposure_prices=exposure_prices,
                    get_executable_quantity=self._get_executable_quantity,
                    validate_intent_prices=self._validate_intent_prices,
                    get_min_notional=self._get_min_notional,
                )
            self._commit_simulated_results(
                cash=staged_cash,
                positions=self._positions,
                result=step_result,
            )
            if step_result.resting_intents:
                resting = list(step_result.resting_intents)
                self._pending_decision = merge_pending_decisions(
                    self._pending_decision,
                    resting,
                    primary_symbol=primary_symbol,
                )
                self._record_resting_since(resting, ts, primary_symbol=primary_symbol)
            self._prune_pending_submissions(primary_symbol=primary_symbol)
            self._apply_financing_cash_flows(ts, raw_bars)
        elif ready_decision and not self._execute_live_decision(
            ready_decision,
            raw_bars,
            ts,
            lagged_adv_by_symbol=lagged_adv_by_symbol,
        ):
            self._commit_cycle_bars(audit_bars, audit_histories, ts)
            return None
        if not execution_already_committed:
            for symbol in resolved_active_symbols:
                self._last_execution_bar_ts[symbol] = ts
                self._execution_bar_filled_quantities[symbol] = (
                    cycle_used_bar_quantity_by_symbol.get(symbol, 0.0)
                )
            self._persist_state()

        evaluated_bars: dict[str, tuple[dict[str, float], float]] = {}
        if self._batch_feature_fn is not None:
            try:
                feature_batch = self._build_feature_batch(raw_frames, resolved_active_symbols, ts)
                featured_batch = evaluate_batch_features(self._batch_feature_fn, feature_batch)
                for subscription in feature_batch.active_primary_subscriptions:
                    featured = featured_batch[subscription]
                    bar = (
                        featured.iloc[-1]
                        .drop(labels=[AVAILABLE_AT_COLUMN], errors="ignore")
                        .to_dict()
                    )
                    price = float(bar.get("close", float("nan")))
                    if not isfinite(price) or price <= 0:
                        raise ValueError(
                            f"{subscription.symbol} feature output has invalid close at {ts}: "
                            f"{price}"
                        )
                    evaluated_bars[subscription.symbol] = (bar, price)
            except Exception:
                logger.exception(
                    "Batch feature processing failed; cycle %s remains uncommitted",
                    ts,
                )
                # Execution of an already-pending order is independent of
                # feature computation. Persist that fill, but leave the market
                # data watermark unchanged so the decision phase is retried.
                self._persist_state()
                raise

        # ── Step 1.5: equity/drawdown check — right after this bar's fills
        # and stops are applied, before the strategy sees the bar. Mirrors
        # the backtest engine's ordering so a drawdown breach halts new
        # entries on the same cycle it's detected, not one cycle later ──
        self._record_equity(ts)
        if self._halted:
            for symbol, position in self._positions.items():
                if symbol in raw_bars:
                    position.periods_held += 1
            # Equity is still marked on this bar, so the bar is recorded too.
            self._commit_cycle_bars(audit_bars, audit_histories, ts)
            return feature_batch.as_of if feature_batch is not None else None

        # A rebalance stopped on this bar is replanned on the next one: the
        # same bar and book would size the same leg and send it again. Kept on
        # the trader, since a cycle that fails after the stop retries the bar.
        if live_rebalance_blocks_decisions or self._rebalance_stopped_at == ts:
            for symbol, position in self._positions.items():
                if symbol in raw_bars:
                    position.periods_held += 1
            self._commit_cycle_bars(audit_bars, audit_histories, ts)
            return feature_batch.as_of if feature_batch is not None else None

        # A declared auxiliary input is read through ctx.market_data, which
        # only the batch-feature path built before. Assemble the same view for
        # a plain feature_fn strategy too: reading a second frequency must not
        # require adopting batch features.
        market_data_view = feature_batch.market_data if feature_batch is not None else None
        if market_data_view is None and self._auxiliary_subscriptions:
            market_data_view = self._build_feature_batch(
                raw_frames, resolved_active_symbols, ts
            ).market_data

        if feature_batch is None:
            if self._feature_fn is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("missing feature callback")
            for symbol, history in histories.items():
                try:
                    featured = _validate_feature_output(
                        self._feature_fn(history),
                        symbol=symbol,
                        event_ts=ts,
                    )
                    bar = (
                        featured.iloc[-1]
                        .drop(labels=[AVAILABLE_AT_COLUMN], errors="ignore")
                        .to_dict()
                    )
                    price = float(bar.get("close", float("nan")))
                    if not isfinite(price) or price <= 0:
                        raise ValueError(
                            f"{symbol} feature output has invalid close at {ts}: {price}"
                        )
                except Exception:
                    logger.exception(
                        "Feature processing failed for %s; cycle %s remains uncommitted",
                        symbol,
                        ts,
                    )
                    # Execution of an already-pending order is independent of
                    # feature computation. Persist that fill, but leave the market
                    # data watermark unchanged so the decision phase is retried.
                    self._persist_state()
                    raise
                evaluated_bars[symbol] = (bar, price)

        # Validate every symbol before publishing feature-derived facts or
        # invoking the strategy, so one malformed plugin result cannot leave a
        # partially observed multi-asset event.
        bars: dict[str, dict[str, float]] = {}
        for symbol, (bar, price) in evaluated_bars.items():
            bars[symbol] = bar
            self._last_prices[symbol] = price

            if self._on_signal_outcome:
                sig = bar.get("entry_signal")
                if sig is not None and not pd.isna(sig) and float(sig) != 0:
                    self._on_signal_outcome(symbol, ts, float(sig), price)
                exit_sig = bar.get("exit_signal")
                if exit_sig is not None and not pd.isna(exit_sig) and float(exit_sig) != 0:
                    self._on_signal_outcome(
                        symbol,
                        ts,
                        float(exit_sig),
                        price,
                        signal_type="exit",
                    )

        # PortfolioWeights and grouped OrderIntents must be immediately
        # executable when returned (validate_strategy_decision enforces
        # this), so on_bar is never gated waiting for one to become ready.
        equity, position_snapshot = self._calc_account_snapshot()
        ctx = Context(
            ts=ts,
            symbol=primary_symbol,
            symbols=self._symbols,
            bar=bars.get(primary_symbol, {}),
            bars=bars,
            positions=position_snapshot,
            account_id=self._account_id,
            account=AccountSnapshot(
                currency=self._currency,
                cash=self._cash,
                equity=equity,
            ),
            period_index=self._period_index,
            decision_at=feature_batch.as_of if feature_batch is not None else None,
            market_data=market_data_view,
        )
        strategy_started = perf_counter()
        try:
            intent = self._strategy.on_bar(ctx)
        finally:
            self._cycle_strategy_seconds += perf_counter() - strategy_started
        validate_strategy_decision(
            intent,
            set(self._symbols),
            primary_symbol=primary_symbol,
            bars=bars,
            positions=self._positions,
            broker_for=self._config.broker_for,
            can_short=self._can_short,
        )
        intent = self._without_halted_account(intent)
        self._period_index += 1
        if self._executor.simulation:
            self._validate_resting_sessions(intent, ts, primary_symbol=primary_symbol)
            self._replace_resting_intents(intent, ts, primary_symbol=primary_symbol)
            self._pending_decision = merge_pending_decisions(
                self._pending_decision,
                intent,
                primary_symbol=primary_symbol,
            )
        else:
            ready_decision, waiting_decision = partition_pending_decision(
                intent,
                raw_bars,
                self._positions,
                primary_symbol=primary_symbol,
            )
            self._pending_decision = merge_pending_decisions(
                self._pending_decision,
                waiting_decision,
                primary_symbol=primary_symbol,
            )
            if ready_decision:
                execution_complete = self._execute_live_decision(
                    ready_decision,
                    raw_bars,
                    ts,
                    lagged_adv_by_symbol=lagged_adv_by_symbol,
                )
                if not execution_complete and self._halted:
                    self._commit_cycle_bars(audit_bars, audit_histories, ts)
                    return None

        for symbol, position in self._positions.items():
            if symbol in raw_bars:
                position.periods_held += 1
        # Record OHLCV after processing (equity already recorded in Step 1.5)
        self._commit_cycle_bars(audit_bars, audit_histories, ts)
        return feature_batch.as_of if feature_batch is not None else None

    def _post_fill_risk_violation(self, *, include_net: bool = True) -> str | None:
        """Validate confirmed exposure after an exposure-increasing fill."""
        equity, _ = self._calc_account_snapshot()
        if not self._positions:
            return None
        if equity <= EPSILON:
            return f"account {self._account_id} confirmed equity is non-positive ({equity:.6f})"
        signed_weights = calculate_position_weights(
            self._positions,
            equity,
            prices=self._last_prices,
            get_cost_model=self._get_cost_model,
        )

        max_position_weight = self._risk_policy.max_position_weight
        if max_position_weight is not None:
            for symbol, weight in signed_weights.items():
                if abs(weight) > max_position_weight + EPSILON:
                    return (
                        f"{symbol} confirmed weight {abs(weight):.6f} exceeds "
                        f"max_position_weight={max_position_weight:.6f}"
                    )
        gross = sum(abs(weight) for weight in signed_weights.values())
        max_gross = self._risk_policy.max_gross_exposure
        if max_gross is not None and gross > max_gross + EPSILON:
            return (
                f"account {self._account_id} confirmed gross exposure {gross:.6f} "
                f"exceeds max_gross_exposure={max_gross:.6f}"
            )
        if include_net:
            net = abs(sum(signed_weights.values()))
            max_net = self._risk_policy.max_net_exposure
            if max_net is not None and net > max_net + EPSILON:
                return (
                    f"account {self._account_id} confirmed absolute net exposure "
                    f"{net:.6f} exceeds max_net_exposure={max_net:.6f}"
                )
        return None

    def _get_last_price(self, sym: str, ps: PositionState) -> float:
        try:
            return self._last_prices[sym]
        except KeyError as exc:
            raise ValueError(f"no current valuation mark for open position {sym}") from exc

    def _get_cost_model(self, sym: str) -> CostModel:
        return self._executor.get_cost_model(sym)

    def _get_executable_quantity(self, symbol: str, quantity: float) -> float:
        """Apply the shared instrument quantity contract before broker preparation."""
        return self._instruments[symbol].normalize_quantity(quantity)

    def _validate_intent_prices(self, symbol: str, intent: OrderIntent) -> None:
        """Apply the shared authoritative price grid before broker preparation."""
        self._instruments[symbol].validate_order_prices(intent)

    def _get_min_notional(self, symbol: str) -> float | None:
        """Return the shared entry-order minimum, when configured."""
        return self._instruments[symbol].min_notional

    def _can_short(self, symbol: str) -> bool:
        """Apply the shared instrument short restriction before broker preparation."""
        return self._instruments[symbol].can_short

    def _apply_financing_cash_flows(
        self,
        ts: datetime,
        bars: Mapping[str, Mapping[str, object]],
    ) -> None:
        """Apply each simulation financing observation at most once.

        Funding and borrow accrue off the same bar event and share one
        per-symbol gate: both are consumed together or not at all.
        """
        eligible_bars = {
            symbol: bar
            for symbol, bar in bars.items()
            if ts
            > self._last_financing_ts.get(
                symbol,
                datetime.min.replace(tzinfo=UTC),
            )
        }
        observed_symbols, cash_flows = calculate_funding_cash_flows(
            ts,
            eligible_bars,
            self._positions,
            get_cost_model=self._get_cost_model,
        )
        borrow_symbols, borrow_cash_flows = calculate_borrow_cash_flows(
            ts,
            eligible_bars,
            self._positions,
            get_cost_model=self._get_cost_model,
        )
        cash_flows.extend(borrow_cash_flows)
        for symbol in (*observed_symbols, *borrow_symbols):
            self._last_financing_ts[symbol] = ts
        for cash_flow in cash_flows:
            self._cash += cash_flow.cash_flow
            if self._on_financing_cash_flow:
                self._on_financing_cash_flow(cash_flow)
        if cash_flows:
            self._performance_dirty = True

    def _calc_account_snapshot(self) -> tuple[float, dict[str, Position]]:
        return calc_equity(
            self._cash,
            self._positions,
            get_price=self._get_last_price,
            get_cost_model=self._get_cost_model,
        )

    def _record_equity(self, ts: datetime) -> None:
        """Calculate equity + drawdown, check the max-drawdown circuit
        breaker, call on_bar callback, and send periodic status."""
        equity, _ = self._calc_account_snapshot()
        self._equity_peak = max(self._equity_peak, equity)
        drawdown = (
            (equity - self._equity_peak) / self._equity_peak if self._equity_peak > 0 else 0.0
        )
        if (
            self._risk_policy.max_drawdown_rate
            and not self._halted
            and drawdown <= -self._risk_policy.max_drawdown_rate
        ):
            self._flatten_account_and_halt(
                ts,
                reason=REASON_DRAWDOWN_BREACH,
                title="Max Drawdown Breach",
                detail=f"drawdown={drawdown:.2%} <= -{self._risk_policy.max_drawdown_rate:.2%}",
            )
            equity, _ = self._calc_account_snapshot()
            drawdown = (
                (equity - self._equity_peak) / self._equity_peak if self._equity_peak > 0 else 0.0
            )

        period_return = equity / self._prev_equity - 1.0 if self._prev_equity > 0 else 0.0
        self._prev_equity = equity
        signed_weights = calculate_position_weights(
            self._positions,
            equity,
            prices=self._last_prices,
            get_cost_model=self._get_cost_model,
        )
        self._portfolio_diagnostics = (
            sum(abs(weight) for weight in signed_weights.values()),
            sum(signed_weights.values()),
            max((abs(weight) for weight in signed_weights.values()), default=0.0),
            self._pending_traded_notional / equity if equity > EPSILON else 0.0,
        )
        self._pending_traded_notional = 0.0
        if self._on_bar:
            gross, net, concentration, turnover = self._portfolio_diagnostics
            self._on_bar(
                self._run_id,
                ts,
                self._account_id,
                self._currency,
                equity,
                drawdown,
                period_return,
                gross,
                net,
                concentration,
                turnover,
            )
        if self._performance_dirty:
            # The current equity point must exist before KPI recomputation.
            if self._on_performance:
                self._on_performance(self._run_id, self._account_id)
            self._performance_dirty = False

        # Periodic status notification (flags cached at init)
        if self._status_interval is not None:
            self._status_period_count += 1
            if self._status_period_count >= self._status_interval:
                num_periods = self._status_period_count
                self._status_period_count = 0
                pos_str = "flat"
                if self._positions:
                    pos_str = ", ".join(
                        f"{position.side} {symbol}" for symbol, position in self._positions.items()
                    )
                self._notify(
                    "send_status",
                    strategy=(
                        f"{self._executor.strategy_name}:{self._account_id}({self._currency})"
                    ),
                    symbol=",".join(self._positions),
                    equity=equity,
                    drawdown=drawdown,
                    period_pnl=equity - self._status_window_equity,
                    num_periods=num_periods,
                    position=pos_str,
                )
                self._status_window_equity = equity

    def _flatten_account_and_halt(
        self, ts: datetime, *, reason: str, title: str, detail: str
    ) -> None:
        """Queue or submit exits for every position, halt, and alert the outcome."""
        exit_queued = False
        submit_failure: Exception | None = None
        held_before = set(self._positions)
        if self._executor.simulation:
            queue_market_exit_all(self._positions, reason=reason)
            flattened = not self._positions
            exit_queued = not flattened
        else:
            actions = [
                OrderIntent(action="close", symbol=symbol, reason=reason)
                for symbol in self._positions
            ]
            reference_bars = {
                symbol: {"close": self._last_prices[symbol]}
                for symbol in self._positions
                if symbol in self._last_prices
            }
            # An exit with no reference price cannot be sized or checked, so it
            # stays open, recorded, instead of failing every other exit.
            for symbol, position in self._positions.items():
                if symbol not in reference_bars:
                    self._report_skipped_close(
                        symbol,
                        quantity=position.quantity,
                        held=position.quantity,
                        ts=ts,
                        reason="close_without_mark",
                        title="Close Without Mark",
                        cause="no current valuation mark to price the exit",
                    )
            # Live emergency exits submit the full remaining quantity. Broker
            # execution reports remain authoritative for partial fills.
            self._flatten_at = ts
            try:
                flattened = self._execute_live_decision(
                    actions,
                    reference_bars,
                    ts,
                    apply_volume_limit=False,
                )
            except Exception as exc:
                if self._halted:
                    # A halt during submission has alerted already.
                    raise
                # Queueing the exits checkpoints them first, so a store
                # outage fails here, before the account is halted.
                submit_failure = exc
                flattened = False
            finally:
                self._flatten_at = None
        self._enter_halt()
        failure, note = self._persist_halt(cancel_on_retry=False)
        if submit_failure is not None:
            outcome = f"exit submission failed ({type(submit_failure).__name__}: {submit_failure})"
        elif flattened and self._positions and set(self._positions) == held_before:
            outcome = f"no position fully closed; {', '.join(sorted(self._positions))} remain open"
        elif flattened and self._positions:
            outcome = f"closed all but {', '.join(sorted(self._positions))}, which remain open"
        elif flattened:
            outcome = "flattened account positions"
        elif exit_queued:
            outcome = "market exits queued for next observed opens"
        elif self._active_orders:
            outcome = "flattening in progress"
        else:
            outcome = "flatten attempt failed"
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] {title}",
            message=f"account_id={self._account_id} {detail} — {outcome} and halted{note}",
        )
        logger.warning(
            "LiveTrader account %s halted at %s: %s %s — %s",
            self._account_id,
            ts,
            title,
            detail,
            outcome,
        )
        self._record_runtime_event("halted", {"reason": title, "message": f"{detail} — {outcome}"})
        if submit_failure is not None:
            raise submit_failure
        if failure is not None:
            raise failure

    def _run_requested_halt(self) -> bool:
        """Apply pending ``request_halt`` calls from the polling thread.

        First in the cycle, so a planned order is never submitted, and a
        pending flatten is refused rather than having its exits cancelled.
        Returns whether it sent a halt's owed cancellation pass.
        """
        reasons = self._drain_halt_requests()
        if not reasons:
            return False
        # A manual halt of a halted account skips _enter_halt, yet drops a reset too.
        self._drop_pending_halt_reset()
        return self._apply_manual_halt("; ".join(reasons))

    def _drain_halt_requests(self) -> list[str]:
        reasons: list[str] = []
        while True:
            try:
                reasons.append(self._halt_requests.get_nowait())
            except Empty:
                return reasons

    def _run_requested_halt_reset(self) -> bool:
        """Apply a pending ``request_halt_reset`` through ``reset_halt``, alerting the outcome.

        Returns whether the account was reset.
        """
        if not self._drain_halt_reset_requests():
            return False
        if not self._halted:
            # A reset would re-base a running account's drawdown peak unasked.
            self._refuse_requested_reset("the account is not halted")
            return False
        title = f"[{self._executor.strategy_name}] Halt Reset"
        resumed = "halt reset; live mode reconciles with the broker before any new decision"
        try:
            self.reset_halt()
        except halt_recovery.HaltResetRefusedError as exc:
            # reset_halt recorded the readiness reason as a runtime event.
            self._refuse_requested_reset(str(exc))
            return False
        except Exception as exc:
            if self._halted:
                self._refuse_requested_reset(f"{type(exc).__name__}: {exc}")
            else:
                # Applied in memory; only its checkpoint failed.
                self._notify(
                    "send_alert",
                    title=title,
                    message=(
                        f"{resumed}; checkpoint not written ({type(exc).__name__}: {exc}), "
                        "so a restart now would resume halted."
                    ),
                )
            raise
        logger.warning("Halt reset by request")
        self._notify("send_alert", title=title, message=f"{resumed}.")
        return True

    def _drop_pending_halt_reset(self) -> None:
        # The order of the two is unknown, and a reset must never undo a later halt.
        if self._drain_halt_reset_requests():
            self._refuse_requested_reset("a halt was applied while the reset was pending")

    def _refuse_requested_reset(self, cause: str) -> None:
        logger.error("Halt reset request refused: %s", cause)
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Halt Reset Refused",
            message=f"{cause}; nothing was reset.",
        )

    def _drain_halt_reset_requests(self) -> bool:
        requested = False
        while True:
            try:
                self._halt_reset_requests.get_nowait()
            except Empty:
                return requested
            requested = True

    def _run_requested_flatten(self) -> None:
        """Act on a pending ``request_flatten`` from the polling thread."""
        with self._flatten_request_lock:
            request = self._flatten_request
        if request is None:
            return
        reason = request[1]
        if self._halted:
            # The refusal says how the wait ended; a resolved one would read as done.
            self._flatten_waits = 0
            self._flatten_alert.retain(())
            self._clear_flatten_request(request)
            logger.error("Operator flatten refused while halted: %s", reason)
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Operator Flatten Refused",
                message=(
                    f"account_id={self._account_id} {reason} — the account is halted, so "
                    "its book may not be safe to trade from; nothing was submitted."
                ),
            )
            return
        if self._resume_unverified:
            # A reset book is not traded on until a reconciliation round
            # matches it; the request waits for that like any decision.
            return
        if not self._executor.simulation and self._active_orders:
            # Exits queue behind working orders, so those are cancelled first;
            # a cancellation the venue has not confirmed retries next cycle.
            timed_out = [
                tracked
                for tracked in self._active_orders
                if tracked.placement_attempted and self._live_order_timed_out(tracked)
            ]
            try:
                self._cancel_active_orders()
            except ReportNotAppliedError as exc:
                # Not a halt: one transient answer would then refuse the
                # flatten, and the cancel goes out again each cycle anyway.
                if exc.order not in self._active_orders:
                    # Its cancel's own answer applied and ended it.
                    self._alert_unapplied(exc)
                else:
                    self._flatten_waits += 1
                    self._alert_unapplied(exc, "; the operator flatten waits for it")
                    return
            finally:
                # Ended here rather than by its timeout's cancel, it fails
                # nothing: the account halts once, with this flatten's alert.
                for tracked in timed_out:
                    if tracked not in self._active_orders and tracked.status != "filled":
                        logger.warning(
                            "%s order_id=%s past its timeout ended %s in the operator flatten",
                            tracked.request.symbol,
                            tracked.order_id or "unassigned",
                            tracked.status,
                        )
            if self._active_orders:
                self._flatten_waits += 1
                summary = self._tracked_orders_summary()
                if self._flatten_waits == 1:
                    logger.info("Operator flatten waits for cancels to be confirmed: %s", summary)
                    return
                # Neither a cycle error nor a timeout will speak for it: the
                # venue may be down, or never confirm a cancel it answered.
                self._flatten_alert.fire(
                    None,
                    "Operator Flatten Waiting",
                    f"account_id={self._account_id} {reason} — no exit goes out until the "
                    f"venue confirms these orders ended: {summary}",
                )
                return
        self._flatten_waits = 0
        self._flatten_alert.resolve(None)
        self._clear_flatten_request(request)
        self._flatten_account_and_halt(
            self._utc_now(),
            reason=REASON_OPERATOR_FLATTEN,
            title="Operator Flatten",
            detail=reason,
        )

    def _flatten_cancels_this_cycle(self) -> bool:
        """Whether a requested flatten will poll and cancel every tracked order.

        Order work it is about to do is left to it, so an order is not
        cancelled twice in one cycle nor reported twice, and nothing halts the
        account ahead of it. A request that lands while the strategy's orders
        go out stops them at the next order, for the flatten to drop; one
        that lands as a flatten submits its exits leaves them going out.
        """
        with self._flatten_request_lock:
            requested = self._flatten_request is not None
        return (
            requested
            and self._flatten_at is None
            and not self._halted
            and not self._resume_unverified
        )

    def _clear_flatten_request(self, handled: tuple[int, str]) -> None:
        # A request that arrived after ``handled`` was read stays pending.
        with self._flatten_request_lock:
            if self._flatten_request == handled:
                self._flatten_request = None
