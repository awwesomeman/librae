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
from threading import Event
from time import perf_counter
from typing import TYPE_CHECKING, Literal

import pandas as pd

from librae.core import EPSILON
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    ExecutionLiquidityUnavailableError,
    ExecutionPriceUnavailableError,
    ExecutionResult,
    ExecutionSideUnavailableError,
    ExecutionUnavailableError,
    RuntimeEvent,
    apply_execution_fill,
    calc_equity,
    calculate_position_weights,
    check_stop_targets,
    execute_order_intents,
    execute_pending_decision_and_stops,
    execute_portfolio_weights,
    merge_pending_decisions,
    normalize_order_intents,
    order_side_is_tradable,
    partition_pending_decision,
    queue_market_exit_all,
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
from librae.core.readiness import evaluate_observation, next_expected_close
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
    require_resting_session_support,
    resting_session_label,
    session_label,
    session_labels,
    validate_calendar_id,
)

from .execution_identity import (
    ExecutionIdentity,
    account_lease_key,
    resolve_execution_identity,
    runtime_state_key,
)
from .executor import ExecutionReport, LiveExecutor, OrderRequest
from .interfaces import (
    BarCallback,
    BarDataFetcher,
    FinancingCashFlowCallback,
    HeartbeatCallback,
    Notifier,
    OhlcvCallback,
    PerformanceCallback,
    PositionEventCallback,
    RuntimeEventCallback,
    SignalOutcomeCallback,
    WarmupFetcher,
)
from .state import (
    LiveRebalance,
    LiveRuntimeState,
    LiveStateStore,
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
            funding = _rates_or_none(
                fetch_funding_rate_history, "funding", instrument.venue_symbol, limit=limit
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
            # Deliberately not the bar limit funding uses. Only the newest
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
    """Fail before polling when a route cannot normalize its requested bars."""
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
        if self._poll_seconds > 0 and self._poll_seconds > self._interval_delta.total_seconds():
            logger.warning(
                "poll_seconds=%s exceeds timeframe=%s (%s seconds); "
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
                    "one live run owns one account and requires one shared order adapter"
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
        if is_live and not getattr(self._state_store, "restart_durable", False):
            # Fail closed on silence: the persistence methods alone are
            # satisfied by a dictionary, so an undeclared store would reach
            # order-capable startup with no recovery state and only reveal it
            # after a crash.
            raise ValueError(
                f"live mode requires a restart-durable state_store; "
                f"{type(self._state_store).__name__} does not declare "
                "restart_durable=True. Use the reference database store, or a "
                "custom store that genuinely survives the process."
            )

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
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
        self._cycle_fetch_seconds: dict[str, float] = {}
        self._cycle_strategy_seconds = 0.0
        self._cycle_order_seconds = 0.0
        self._last_cycle_diagnostics: CycleDiagnostics | None = None
        self._warmup_requested_periods: dict[str, int] = {}
        self._warmup_fetch_attempts: dict[str, int] = {}
        self._warmup_exhausted_fingerprints: dict[str, frozenset[tuple[int, int]]] = {}
        self._replay_backlog_exhausted: dict[str, tuple[int, int]] = {}
        self._reported_warmup_reasons: dict[str, str] = {}
        self._lease_acquired = False
        self._account_lease_acquired = False
        self._restored_state = False
        self._on_runtime_event = on_runtime_event
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
        self._on_ohlcv = on_ohlcv
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
        self._stop_event = Event()
        self._sleep = self._stop_event.wait  # instance attribute so tests can skip real delays
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
        """Stamp the event an intent first rested on, once."""
        if isinstance(decision, PortfolioWeights):
            return
        for intent in decision:
            self._pending_resting_since.setdefault(intent.symbol or primary_symbol, ts)

    def _replace_resting_intents(
        self,
        new_decision: StrategyDecision,
        ts: datetime,
        *,
        primary_symbol: str,
    ) -> None:
        """Let a new decision cancel and replace an order still resting.

        Mirrors the backtest: ``gtc`` commits an order until it fills, so the
        strategy's next decision for that symbol is its way out. Only an intent
        that has actually rested is replaceable — one waiting for its symbol's
        first bar keeps the existing duplicate guard.
        """
        if not new_decision or isinstance(self._pending_decision, PortfolioWeights):
            return
        replacing = (
            {intent.symbol or primary_symbol for intent in new_decision}
            if not isinstance(new_decision, PortfolioWeights)
            else None  # a whole-book target supersedes every resting order
        )
        live: list[OrderIntent] = []
        for intent in self._pending_decision:
            symbol = intent.symbol or primary_symbol
            resting = symbol in self._pending_resting_since
            if resting and (replacing is None or symbol in replacing):
                self._pending_resting_since.pop(symbol, None)
                if self._on_runtime_event:
                    self._on_runtime_event(
                        RuntimeEvent(
                            ts=ts,
                            event_type="decision_skipped",
                            symbol=symbol,
                            detail={"reason": "resting_order_replaced"},
                        )
                    )
                continue
            live.append(intent)
        self._pending_decision = live

    def _validate_resting_sessions(
        self,
        decision: StrategyDecision,
        ts: datetime,
        *,
        primary_symbol: str,
    ) -> None:
        """Fail on the emitting event, not mid-run, when a day limit has no
        session to expire against. This is engine expressibility, not a venue
        rule, so it belongs with the decision that requested it. Only calendar
        presence is checked — labelling this event would reject a decision
        emitted during another instrument's session."""
        del ts
        if isinstance(decision, PortfolioWeights):
            return
        for intent in decision:
            if intent.time_in_force == "day" and intent.limit_price is not None:
                symbol = intent.symbol or primary_symbol
                try:
                    require_resting_session_support(
                        calendar_id=self._instruments[symbol].calendar_id,
                        timeframe=self._config.timeframe,
                    )
                except ValueError as exc:
                    raise ValueError(f"{symbol}: {exc}") from exc

    def _prune_pending_submissions(self, *, primary_symbol: str) -> None:
        """Forget submissions whose intent is no longer pending."""
        if isinstance(self._pending_decision, PortfolioWeights):
            self._pending_resting_since = {}
            return
        still_pending = {intent.symbol or primary_symbol for intent in self._pending_decision}
        self._pending_resting_since = {
            symbol: submitted_at
            for symbol, submitted_at in self._pending_resting_since.items()
            if symbol in still_pending
        }

    def _session_of(self, symbol: str, ts: datetime) -> object:
        """Session label a resting ``day`` order expires against."""
        try:
            return resting_session_label(
                ts,
                calendar_id=self._instruments[symbol].calendar_id,
                timeframe=self._config.timeframe,
            )
        except ValueError as exc:
            raise ValueError(f"{symbol}: {exc}") from exc

    def _expire_resting_day_intents(
        self,
        ts: datetime,
        priced_symbols: Container[str],
        *,
        primary_symbol: str,
    ) -> None:
        """Drop resting ``day`` limits once their submitting session has ended.

        Mirrors the backtest so a deterministic runtime cannot drift on this
        sequence. Only a limit order can outlive an event, so a market order
        never consults the calendar.
        """
        if isinstance(self._pending_decision, PortfolioWeights) or not self._pending_decision:
            return
        live: list[OrderIntent] = []
        for intent in self._pending_decision:
            symbol = intent.symbol or primary_symbol
            submitted_at = self._pending_resting_since.get(symbol)
            if (
                intent.time_in_force == "day"
                and intent.limit_price is not None
                and submitted_at is not None
                and symbol in priced_symbols
                and self._session_of(symbol, ts) != self._session_of(symbol, submitted_at)
            ):
                self._pending_resting_since.pop(symbol, None)
                if self._on_runtime_event:
                    self._on_runtime_event(
                        RuntimeEvent(
                            ts=ts,
                            event_type="decision_skipped",
                            symbol=symbol,
                            detail={"reason": "day_order_expired"},
                        )
                    )
                continue
            live.append(intent)
        self._pending_decision = live

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
        self._active_orders = state.active_orders
        self._live_rebalance = state.live_rebalance
        self._equity_peak = state.equity_peak
        self._prev_equity = state.prev_equity
        self._status_window_equity = state.status_window_equity
        self._trade_count = state.trade_count
        self._event_sequence = state.event_sequence
        self._period_index = state.period_index
        self._status_period_count = state.status_period_count
        self._halted = state.halted
        self._adv_session_labels = state.adv_session_labels
        self._adv_filled_quantities = state.adv_filled_quantities
        self._restored_state = True
        logger.info(
            "Restored runtime state: key=%s run_id=%s cycle=%s orders=%d halted=%s",
            self._state_key,
            self._run_id,
            self._last_cycle_ts,
            len(self._active_orders),
            self._halted,
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
                    },
                )
            )

    def _persist_state(self, *orders: TrackedOrder) -> None:
        """Critical checkpoint write; failures propagate and stop the cycle."""
        if self._state_store is not None:
            self._state_store.save(self._snapshot_state(), orders)

    # WHY: 3 consecutive errors likely means a persistent issue (API down, DB
    # unreachable), not a transient blip — worth alerting the operator.
    CONSECUTIVE_ERROR_THRESHOLD = 3

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
        """Alert if the next observation is overdue against its own calendar.

        Catches a feed that stops updating without ever raising an exception
        (CONSECUTIVE_ERROR_THRESHOLD only covers raised errors). The boundary
        is the next bar's expected close for this subscription's own timeframe
        and calendar, so a closed market is not mistaken for a dead feed and a
        same-symbol H1 input is not judged on a D1 cadence. Edge-triggered:
        alerts once when crossing into stale and re-arms once fresh data
        resumes. Returns whether the frame is stale so live fails closed.
        """
        subscription = self._market_data_subscriptions[symbol]
        status = evaluate_observation(
            latest_ts,
            as_of=self._utc_now(),
            timeframe=subscription.timeframe,
            calendar_id=subscription.calendar_id,
            grace=self._staleness_grace(subscription),
        )
        if not status.calendar_anchored and subscription not in self._unanchored_reported:
            self._unanchored_reported.add(subscription)
            logger.warning(
                "Freshness for %s is not calendar-anchored: %s has no session in %s, "
                "so weekend and holiday awareness is lost for this subscription",
                symbol,
                latest_ts,
                subscription.calendar_id,
            )
        is_stale = not status.fresh
        was_stale = self._stale_alerted.get(subscription, False)

        if is_stale and not was_stale:
            self._stale_alerted[subscription] = True
            logger.warning(
                "Stale data: %s latest bar %s, next observation was due %s",
                symbol,
                latest_ts,
                status.due_at,
            )
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Stale Data: {symbol}",
                message=(
                    f"Latest bar is {latest_ts}; the next observation was due "
                    f"{status.due_at} — feed may have stopped updating."
                ),
            )
        elif not is_stale and was_stale:
            self._stale_alerted[subscription] = False
            logger.info("Stale data recovered: %s", symbol)
        return is_stale

    def _report_data_readiness(self, missing: Sequence[str]) -> bool:
        """Report absent inputs and say whether evaluation must be held.

        Edge-triggered like staleness: one diagnostic when data readiness is
        lost and one when it returns, not one per poll cycle. Optional
        subscriptions are reported and stepped over; required ones hold the
        strategy.
        """
        blocking = [symbol for symbol in missing if symbol not in self._optional_symbols]
        omitted = [symbol for symbol in missing if symbol in self._optional_symbols]
        if omitted:
            logger.info("Proceeding without optional market data: %s", ", ".join(omitted))
        if blocking and not self._data_gap_alerted:
            self._data_gap_alerted = True
            logger.warning(
                "Data not ready; holding strategy evaluation for %s", ", ".join(blocking)
            )
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Market Data Not Ready",
                message=(
                    f"No usable observation for required {', '.join(blocking)}; "
                    "strategy evaluation is held until the feed recovers."
                ),
            )
        elif not blocking and self._data_gap_alerted:
            self._data_gap_alerted = False
            logger.info("Market data ready again; resuming strategy evaluation")
        return bool(blocking)

    def _refresh_auxiliary_cache(self) -> None:
        """Refresh every auxiliary input through its symbol's own source.

        Auxiliaries produce no execution events, so they carry no durable
        watermark and no fill dedup: a restart simply refetches them. Nothing
        here may abort the cycle — the primary still has to execute — so a
        failed fetch, or a frame that fails normalization, logs and keeps the
        previous frame. Freshness is still evaluated and alerted; it just never
        holds the run, because an auxiliary is not an input the run executes on.
        """
        now = self._utc_now()
        for subscription, owner in self._auxiliary_subscriptions.items():
            if not self._auxiliary_fetch_due(subscription, now):
                continue
            try:
                fetched = self._fetchers[owner](
                    owner,
                    subscription.timeframe,
                    self._feature_history_limit + 1,
                    drop_incomplete=True,
                )
                if fetched is None or fetched.empty:
                    continue
                normalized = self._normalize_runtime_rows(owner, fetched, subscription=subscription)
            except Exception:
                logger.exception(
                    "Failed to refresh auxiliary %s %s; keeping the previous frame",
                    owner,
                    subscription.timeframe,
                )
                continue
            self._auxiliary_cache[subscription] = normalized

        # Evaluate every declared auxiliary from its cache, whatever the fetch
        # did. Checking only after a successful fetch inspects the feed exactly
        # when it is healthy enough to answer and never when it is not, so the
        # two ordinary ways a feed dies — raising and returning nothing — would
        # age the cache silently while the strategy still reads it as context.
        # This also covers a subscription whose refetch was skipped as not due.
        for subscription, owner in self._auxiliary_subscriptions.items():
            self._check_auxiliary_freshness(subscription, owner, as_of=now)

    def _auxiliary_fetch_due(
        self,
        subscription: MarketDataSubscription,
        now: datetime,
    ) -> bool:
        """Skip a refetch until a new observation could exist.

        A daily auxiliary on an hourly run would otherwise be pulled once per
        poll for a bar that cannot change. The calendar already knows when the
        next one is due.
        """
        cached = self._auxiliary_cache.get(subscription)
        if cached is None or cached.empty:
            return True
        last_ts = pd.Timestamp(cached["ts"].iloc[-1]).to_pydatetime()
        try:
            return now >= next_expected_close(
                last_ts,
                timeframe=subscription.timeframe,
                calendar_id=subscription.calendar_id,
            )
        except ValueError:
            return True

    def _check_auxiliary_freshness(
        self,
        subscription: MarketDataSubscription,
        owner: str,
        *,
        as_of: datetime,
    ) -> None:
        """Alert on a stalled auxiliary feed without ever holding the run.

        A dead auxiliary that goes on serving yesterday's frame is the silent
        failure this work exists to close; it just is not grounds to stop
        executing on the primary.

        A subscription that has never delivered is reported once and not
        alerted: there is no observation for it to be late relative to, which
        is the distinction ``ObservationStatus.late`` draws.
        """
        frame = self._auxiliary_cache.get(subscription)
        if frame is None or frame.empty:
            if subscription not in self._auxiliary_never_delivered:
                self._auxiliary_never_delivered.add(subscription)
                logger.warning(
                    "Auxiliary %s %s has never delivered an observation",
                    owner,
                    subscription.timeframe,
                )
            return
        self._auxiliary_never_delivered.discard(subscription)
        last_ts = pd.Timestamp(frame["ts"].iloc[-1]).to_pydatetime()
        status = evaluate_observation(
            last_ts,
            as_of=as_of,
            timeframe=subscription.timeframe,
            calendar_id=subscription.calendar_id,
            grace=self._staleness_grace(subscription),
        )
        was_stale = self._stale_alerted.get(subscription, False)
        if not status.fresh and not was_stale:
            self._stale_alerted[subscription] = True
            logger.warning(
                "Stale auxiliary data: %s %s latest bar %s, next observation was due %s",
                owner,
                subscription.timeframe,
                last_ts,
                status.due_at,
            )
            self._notify(
                "send_alert",
                title=(
                    f"[{self._executor.strategy_name}] Stale Auxiliary Data: "
                    f"{owner} {subscription.timeframe}"
                ),
                message=(
                    f"Latest {subscription.timeframe} bar is {last_ts}; the next was due "
                    f"{status.due_at}. Strategy execution continues on the primary cadence."
                ),
            )
        elif status.fresh and was_stale:
            self._stale_alerted[subscription] = False
            logger.info("Auxiliary data recovered: %s %s", owner, subscription.timeframe)

    def _staleness_grace(self, subscription: MarketDataSubscription) -> timedelta:
        """Bounded publication slack allowed after an expected close.

        Wall-clock on purpose: it models a feed publishing a completed bar
        late, not extra market time. Sized from the subscription's own
        interval so a D1 input is not held to an H1 deadline.
        """
        from librae.core.utils import interval_to_timedelta

        return self.STALE_DATA_TOLERANCE_BARS * interval_to_timedelta(subscription.timeframe)

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
                )
            except Exception as exc:
                frame = self._ohlcv_cache.get(symbol)
                return _MarketDataFetchResult(
                    outcome="error",
                    frame=frame,
                    elapsed_seconds=perf_counter() - started,
                    audit_rows=tuple(audit_rows),
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
            )

        if self._market_data_workers == 1 or len(self._symbols) == 1:
            results = {symbol: fetch_one(symbol) for symbol in self._symbols}
        else:
            worker_count = min(self._market_data_workers, len(self._symbols))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {symbol: pool.submit(fetch_one, symbol) for symbol in self._symbols}
                results = {symbol: futures[symbol].result() for symbol in self._symbols}

        for symbol, result in results.items():
            self._cycle_fetch_seconds[symbol] = result.elapsed_seconds
            if result.outcome == "error":
                assert result.error is not None
                self._record_market_data_fetch_failure(symbol, result.error)
            elif result.outcome == "success":
                self._record_market_data_fetch_success(symbol)
            if self._on_ohlcv is not None:
                audit_by_version: dict[tuple[int, int], _OhlcvAuditRow] = {}
                for event_ts, audit_bar in result.audit_rows:
                    version = (
                        pd.Timestamp(event_ts).value,
                        pd.Timestamp(audit_bar[AVAILABLE_AT_COLUMN]).value,
                    )
                    audit_by_version.setdefault(version, (event_ts, audit_bar))
                for event_ts, audit_bar in (
                    audit_by_version[version] for version in sorted(audit_by_version)
                ):
                    self._on_ohlcv(symbol, self._timeframe, audit_bar, event_ts)
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
        semantics cannot safely replace the local accounting ledger.
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
        self._last_reconciliation_at = now
        try:
            broker_positions = self._read_broker_positions()
            if not self._position_books_match(self._positions, broker_positions):
                self._halt_live(
                    title="Periodic Position Reconciliation Mismatch",
                    message="Local and broker positions differ for configured symbols",
                )
                return
            self._reconcile_open_orders()
            if not self._halted:
                self._reconcile_cash()
        except Exception as exc:
            logger.exception("Periodic broker reconciliation failed")
            self._halt_live(
                title="Periodic Reconciliation Failed",
                message=str(exc),
            )

    def _halt_live(self, *, title: str, message: str) -> None:
        """Fail closed and cancel every tracked order that may still execute."""
        self._halted = True
        self._pending_decision = []
        self._pending_resting_since = {}
        self._live_rebalance = None
        if not self._executor.simulation:
            self._cancel_active_orders()
        self._persist_state()
        logger.error("%s: %s", title, message)
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] {title}",
            message=f"{message}; trading halted.",
        )

    def _fail_group_or_halt(self, tracked: TrackedOrder, *, title: str, message: str) -> bool:
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

        A leg with no group_id has no isolation boundary, so it always halts.
        A grouped leg's confirmed failure cancels only that group's other
        still-active legs and alerts — unrelated groups and independent
        intents keep executing. Returns True if the whole account was halted
        (caller should stop advancing this tick), False if only the group
        was cancelled (caller should continue to the next active order).
        """
        group_id = tracked.request.group_id
        if group_id is None:
            self._halt_live(title=title, message=message)
            return True
        if not self._executor.simulation:
            for sibling in list(self._active_orders):
                if sibling.request.group_id == group_id:
                    self._cancel_tracked_order(sibling)
        logger.error("%s: %s", title, message)
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] {title}",
            message=f"{message}; group {group_id!r} cancelled, other groups unaffected.",
        )
        return False

    def _has_active_recovery_orders(self) -> bool:
        """Whether every tracked order is an engine-owned recovery order."""
        return bool(self._active_orders) and all(
            tracked.request.reason == REASON_DRAWDOWN_BREACH
            and tracked.request.position_effect in ("reduce", "close")
            for tracked in self._active_orders
        )

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
            try:
                if self._halted and not self._has_active_recovery_orders():
                    self._cancel_active_orders()
                else:
                    self._advance_live_orders(submit_planned=False)
                self._reconcile_open_orders()
            except Exception as exc:
                logger.exception("Broker order reconciliation failed")
                self._halt_live(
                    title="Order Reconciliation Failed",
                    message=str(exc),
                )

        self._reconcile_positions()
        self._reconcile_cash()
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
        self._stop_event.clear()
        self._running = True
        self._setup_signal_handlers()
        try:
            self._initialize_run()
            if self._on_ready:
                self._on_ready(self._run_id)
        except BaseException:
            self._running = False
            self._release_lease()
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
                cycle_started_at = self._utc_now()
                cycle_started = perf_counter()
                self._cycle_fetch_seconds = {}
                self._cycle_strategy_seconds = 0.0
                self._cycle_order_seconds = 0.0
                try:
                    self._poll_cycle()
                    self._consecutive_errors = 0
                except Exception:
                    self._consecutive_errors += 1
                    logger.exception(
                        "Error in poll cycle (%d consecutive), will retry next interval",
                        self._consecutive_errors,
                    )
                    if self._consecutive_errors == self.CONSECUTIVE_ERROR_THRESHOLD:
                        self._notify(
                            "send_alert",
                            title=f"[{strategy_name}] Poll Error",
                            message=f"{self._consecutive_errors} consecutive failures. Check logs.",
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
            try:
                self._notify(
                    "send_shutdown",
                    strategy=strategy_name,
                    symbol=symbols_str,
                    reason=shutdown_reason,
                )
                self._notify_pool.shutdown(wait=True)
            finally:
                self._release_lease()
            logger.info("LiveTrader stopped (reason: %s)", shutdown_reason)

    def stop(self) -> None:
        """Signal the runner to stop after the current cycle."""
        self._running = False
        self._stop_event.set()

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
    def run_id(self) -> str:
        """Stable id used by callbacks and persisted runtime facts."""
        return self._run_id

    def halt(self, reason: str = "operator requested halt") -> None:
        """Fail closed immediately until an operator calls ``reset_halt``."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("halt reason must be a non-empty string")
        self._halt_live(title="Manual Halt", message=reason.strip())

    def reset_halt(self) -> None:
        """Start a new risk epoch after operator review."""
        if self._active_orders:
            raise RuntimeError("cannot reset halt while broker orders remain unresolved")
        equity, _ = self._calc_account_snapshot()
        self._halted = False
        self._equity_peak = equity
        self._prev_equity = equity
        self._status_window_equity = equity
        self._persist_state()

    def _setup_signal_handlers(self) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""

        def _handler(signum: int, frame: types.FrameType | None) -> None:
            logger.info("Received signal %d, shutting down gracefully", signum)
            self.stop()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def _poll_cycle(self) -> None:
        """Process completed market-data events without live catch-up orders."""
        if not self._executor.simulation and self._active_orders:
            self._advance_live_orders()
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
            # warmup_ready no longer waits on an optional symbol, so an optional
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
        # active order so a later bar cannot create a conflicting order queue.
        if not self._executor.simulation and (self._active_orders or self._halted):
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

        for cycle_ts in sorted(candidate_symbols_by_timestamp):
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
    ) -> pd.DataFrame | None:
        """Fetch and cache one symbol, propagating failures to the poll coordinator."""
        cached = self._ohlcv_cache.get(symbol)
        if cached is not None and not cached.empty:
            # Restored and test-injected legacy caches may predate row-version
            # metadata. Normalize them before comparing history fingerprints.
            cached = self._normalize_runtime_rows(symbol, cached)
        restart_audit_watermark = (
            self._last_bar_ts.get(symbol)
            if audit_sink is not None and (cached is None or cached.empty)
            else None
        )
        if symbol in self._replay_backlog_exhausted:
            return cached
        if self._warmup_gap(symbol, cached) is not None:
            merged = cached
            base_request = self._warmup_periods + 1
            all_request_sizes = [
                base_request * (2**attempt) for attempt in range(self.WARMUP_MAX_FETCH_ATTEMPTS)
            ]
            request_sizes = all_request_sizes
            try:
                exhausted_fingerprint = self._warmup_exhausted_fingerprints.get(symbol)
                if (
                    exhausted_fingerprint is None
                    and self._warmup_requested_periods.get(symbol) == all_request_sizes[-1]
                ):
                    # A prior largest-rung response added history but did not
                    # close the gap. Re-probe that bound directly; exhaustion
                    # is only established when this rung itself plateaus.
                    request_sizes = [all_request_sizes[-1]]
                if exhausted_fingerprint is not None:
                    probe_periods = self._warmup_requested_periods.get(symbol, base_request)
                    probe = self._fetch_history(symbol, probe_periods)
                    probe = self._eligible_runtime_rows(symbol, probe)
                    if not probe.empty:
                        validate_ohlcv_values(probe, context=f"{symbol} runtime data")
                        merged = self._merge_runtime_rows(
                            symbol,
                            merged,
                            probe,
                            audit_sink=audit_sink,
                            restart_audit_watermark=restart_audit_watermark,
                        )
                    if self._history_fingerprint(merged) == exhausted_fingerprint:
                        return cached
                    self._warmup_exhausted_fingerprints.pop(symbol, None)
                    if self._warmup_gap(symbol, merged) is None:
                        request_sizes = []
                    else:
                        request_sizes = [
                            requested
                            for requested in all_request_sizes
                            if requested > probe_periods
                        ]

                for requested_periods in request_sizes:
                    attempt = all_request_sizes.index(requested_periods) + 1
                    self._warmup_requested_periods[symbol] = requested_periods
                    self._warmup_fetch_attempts[symbol] = attempt
                    before = self._history_fingerprint(merged)
                    new_df = self._fetch_history(symbol, requested_periods)
                    new_df = self._eligible_runtime_rows(symbol, new_df)
                    if not new_df.empty:
                        validate_ohlcv_values(new_df, context=f"{symbol} runtime data")
                        merged = self._merge_runtime_rows(
                            symbol,
                            merged,
                            new_df,
                            audit_sink=audit_sink,
                            restart_audit_watermark=restart_audit_watermark,
                        )
                    if self._warmup_gap(symbol, merged) is None:
                        self._warmup_exhausted_fingerprints.pop(symbol, None)
                        break
                    if (
                        requested_periods == all_request_sizes[-1]
                        and self._history_fingerprint(merged) == before
                    ):
                        self._warmup_exhausted_fingerprints[symbol] = before
                        break
            except Exception:
                self._store_runtime_cache(symbol, merged)
                raise
        else:
            self._warmup_exhausted_fingerprints.pop(symbol, None)
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
                restart_audit_watermark=restart_audit_watermark,
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
        restart_audit_watermark: datetime | None = None,
    ) -> pd.DataFrame:
        """Merge causal row versions, then validate the whole cache cadence."""
        fetched = self._normalize_runtime_rows(symbol, fetched)
        accepted_audit_rows: list[_OhlcvAuditRow] = []
        if audit_sink is not None and restart_audit_watermark is not None:
            replay_rows = fetched.loc[fetched["ts"] <= pd.Timestamp(restart_audit_watermark)]
            accepted_audit_rows.extend(
                (
                    pd.Timestamp(row["ts"]).to_pydatetime(),
                    row.drop(labels="ts").to_dict(),
                )
                for _, row in replay_rows.iterrows()
            )
        if cached is None or cached.empty:
            merged = fetched
        else:
            cached = self._normalize_runtime_rows(symbol, cached)
            if audit_sink is not None and restart_audit_watermark is None:
                watermark = self._last_bar_ts.get(symbol)
                if watermark is not None:
                    previous_availability = cached.set_index("ts")[AVAILABLE_AT_COLUMN]
                    previous_versions = fetched["ts"].map(previous_availability)
                    corrections = fetched.loc[
                        previous_versions.notna()
                        & (fetched[AVAILABLE_AT_COLUMN] > pd.DatetimeIndex(previous_versions))
                        & (fetched["ts"] <= pd.Timestamp(watermark))
                    ]
                    accepted_audit_rows.extend(
                        (
                            pd.Timestamp(row["ts"]).to_pydatetime(),
                            row.drop(labels="ts").to_dict(),
                        )
                        for _, row in corrections.iterrows()
                    )
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
        return normalized

    def _report_incomplete_warmup(self) -> None:
        """Publish an edge-triggered diagnostic after bounded backfill fails."""
        changed: list[str] = []
        for symbol in self._symbols:
            frame = self._ohlcv_cache.get(symbol)
            gap_reason = self._warmup_gap(symbol, frame)
            backlog = self._replay_backlog_exhausted.get(symbol)
            exhausted = symbol in self._warmup_exhausted_fingerprints
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
            if (
                previous_reason == "warmup_backfill_exhausted"
                and not exhausted
                and gap_reason is not None
            ):
                # A successful re-probe is progress, not a new outward edge.
                # Keep the existing incident open until readiness or another
                # terminal plateau rather than alternating alert reasons.
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
                    + "; strategy evaluation is disabled; exhausted sources resume escalation "
                    "only after new history, while replay overflow requires "
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
    ) -> list[OrderRequest]:
        """Size intent at the latest completed close without inventing fills."""
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
                )
            except ExecutionPriceUnavailableError as exc:
                if unavailable_side_symbols:
                    raise ExecutionSideUnavailableError(sorted(unavailable_side_symbols)) from exc
                raise
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
            unit_positions = deepcopy(staged_positions) if grouped else staged_positions
            unit_cash = planning_cash
            unit_exposure_prices = (
                dict(planning_exposure_prices) if grouped else planning_exposure_prices
            )
            unit_bar_quantities = (
                dict(planned_bar_quantity_by_symbol) if grouped else planned_bar_quantity_by_symbol
            )
            unit_adv_quantities = (
                dict(planned_adv_quantity_by_symbol) if grouped else planned_adv_quantity_by_symbol
            )
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
                    prepared = prepare_and_validate(
                        request,
                        reference_price=reference_price,
                    )
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
        required_symbols = set(self._positions) | {
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
        """Advance serial target legs using only one current market snapshot."""
        while self._live_rebalance is not None and not self._active_orders and not self._halted:
            if not self._queue_next_live_rebalance_order(
                bars,
                ts,
                lagged_adv_by_symbol=lagged_adv_by_symbol,
            ):
                break
            self._advance_live_orders()
        return self._live_rebalance is None and not self._active_orders and not self._halted

    def _timed_order_call(
        self,
        callback: Callable[[], ExecutionReport | None],
    ) -> ExecutionReport | None:
        started = perf_counter()
        try:
            return callback()
        finally:
            self._cycle_order_seconds += perf_counter() - started

    def _advance_live_orders(self, *, submit_planned: bool = True) -> None:
        """Poll or submit the head order; dependent orders stay serialized."""
        while self._active_orders and (
            not self._halted
            or self._has_active_recovery_orders()
            or self._active_orders[0].cancel_requested
            or self._active_orders[0].status == "cancel_pending"
        ):
            tracked = self._active_orders[0]
            request = tracked.request
            if tracked.cancel_requested or tracked.status == "cancel_pending":
                self._cancel_tracked_order(tracked)
                return
            if not tracked.placement_attempted:
                if not submit_planned:
                    return
                # Persist placement-attempted before network I/O. A crash in
                # the following call is recovered by client-order lookup and
                # never blindly retried.
                attempted_at = self._clock()
                if attempted_at.tzinfo is None:
                    raise ValueError("clock must return a timezone-aware datetime")
                tracked.placement_attempted = True
                tracked.placement_attempted_at = attempted_at.astimezone(UTC)
                self._persist_state(tracked)
                report = self._timed_order_call(
                    lambda request=request: self._executor.submit_order(request)
                )
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
                except Exception as exc:
                    if not self._live_order_timed_out(tracked):
                        raise
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

            cancellation_was_pending = tracked.status == "cancel_pending"
            prior_filled_quantity = tracked.filled_quantity
            self._apply_order_report(tracked, report)
            if (
                cancellation_was_pending or report.status == "cancel_pending"
            ) and report.status not in (
                "filled",
                "cancelled",
                "rejected",
            ):
                tracked.status = "cancel_pending"
                self._persist_state(tracked)
                return
            if report.status in ("cancelled", "rejected"):
                if self._fail_group_or_halt(
                    tracked,
                    title=f"Order {report.status.title()}",
                    message=(
                        f"{request.symbol} order_id={report.order_id or 'unassigned'} "
                        f"filled={report.filled_quantity:.4f}/"
                        f"{report.requested_quantity:.4f}"
                    ),
                ):
                    return
                continue
            if (
                report.filled_quantity > prior_filled_quantity + EPSILON
                and request.position_effect in ("open", "add")
            ):
                risk_violation = self._post_fill_risk_violation()
                if risk_violation is not None:
                    self._halt_live(
                        title="Post-fill Risk Breach",
                        message=risk_violation,
                    )
                    return
            if report.status != "filled" and self._live_order_timed_out(tracked):
                self._cancel_timed_out_order(tracked, report)
                return
            if report.status != "filled":
                return

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
            cancel_report = self._timed_order_call(
                lambda: self._executor.cancel_order(request, order_id)
            )
            if cancel_report is None:
                raise RuntimeError("broker cancellation returned no execution report")
            self._apply_order_report(tracked, cancel_report)
        except Exception as exc:
            # The cancel attempt itself failed — still unknown broker state.
            self._halt_live(
                title="Order Timeout Cancellation Failed",
                message=(
                    f"{request.symbol} order_id={order_id} could not be confirmed cancelled: {exc}"
                    + (f"; latest status error: {status_error}" if status_error else "")
                ),
            )
            return

        if cancel_report.status == "filled":
            return
        status = cancel_report.status
        message = (
            f"{request.symbol} order_id={order_id} status={status} "
            f"filled={cancel_report.filled_quantity:.4f}/"
            f"{cancel_report.requested_quantity:.4f}"
        )
        if status in ("cancelled", "rejected"):
            self._fail_group_or_halt(tracked, title="Order Timeout", message=message)
        else:
            # Cancellation itself didn't reach a confirmed terminal status —
            # still unknown broker state, always halt.
            tracked.status = cancel_report.status
            self._persist_state(tracked)
            self._halt_live(title="Order Timeout Cancellation Unresolved", message=message)

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
            delta_slippage = report.slippage - tracked.slippage
            delta_tax = report.tax - tracked.tax
            if delta_slippage < -EPSILON or delta_tax < -EPSILON:
                raise ValueError("broker cumulative execution costs moved backwards")
            fill = Fill(
                symbol=report.symbol,
                side="long" if report.side == "buy" else "short",
                price=delta_notional / delta_quantity,
                quantity=delta_quantity,
                commission=report.commission - tracked.commission,
                slippage=max(0.0, delta_slippage),
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
            self._record_adv_fill(report.symbol, delta_quantity, report.executed_at)
            if self._live_rebalance is not None:
                consumed = self._live_rebalance.filled_bar_quantity_by_symbol
                consumed[report.symbol] = consumed.get(report.symbol, 0.0) + delta_quantity

        tracked.order_id = report.order_id or tracked.order_id
        tracked.status = report.status
        tracked.filled_quantity = report.filled_quantity
        tracked.filled_notional = cumulative_notional
        tracked.commission = report.commission
        tracked.slippage = report.slippage
        tracked.tax = report.tax
        tracked.executed_at = report.executed_at or tracked.executed_at
        if report.status in ("filled", "cancelled", "rejected"):
            self._active_orders.remove(tracked)
        if result is not None:
            self._trade_count += len(result.trades)
        self._persist_state(tracked)
        if result is not None:
            self._publish_action_results(result)

    def _cancel_tracked_order(self, tracked: TrackedOrder) -> None:
        """Reconcile and cancel one tracked order without resubmitting it."""
        if not tracked.placement_attempted:
            tracked.status = "cancelled"
            self._active_orders.remove(tracked)
            self._persist_state(tracked)
            return

        cancellation_acknowledged = tracked.status == "cancel_pending"
        if not tracked.cancel_requested:
            tracked.cancel_requested = True
            self._persist_state(tracked)

        try:
            report = self._timed_order_call(
                lambda request=tracked.request, order_id=tracked.order_id: (
                    self._executor.get_order(request, order_id)
                    if order_id
                    else self._executor.find_order(request)
                )
            )
            if report is None:
                logger.error(
                    "Cannot cancel unresolved order %s",
                    tracked.request.client_order_id,
                )
                return
        except Exception:
            logger.exception(
                "Failed to reconcile tracked order before cancellation %s",
                tracked.request.client_order_id,
            )
            return

        self._apply_order_report(tracked, report)
        if report.status in ("filled", "cancelled", "rejected"):
            return
        if cancellation_acknowledged or report.status == "cancel_pending":
            if report.status != "cancel_pending":
                tracked.status = "cancel_pending"
                self._persist_state(tracked)
            return

        try:
            cancel_report = self._timed_order_call(
                lambda request=tracked.request, order_id=report.order_id: (
                    self._executor.cancel_order(request, order_id)
                )
            )
            if cancel_report is None:
                raise RuntimeError("broker cancellation returned no execution report")
        except Exception:
            logger.exception(
                "Failed to cancel tracked order %s",
                tracked.request.client_order_id,
            )
            return

        self._apply_order_report(tracked, cancel_report)

    def _cancel_active_orders(self) -> None:
        """Best-effort cancellation used whenever live trading halts."""
        for tracked in list(self._active_orders):
            self._cancel_tracked_order(tracked)

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

    def _reset_adv_session(self, symbol: str, ts: datetime) -> None:
        """Reset one symbol's cumulative ADV usage when its session changes."""
        if self._adv_lookback_sessions is None:
            return
        if self._interval_delta.days >= 1:
            label = pd.Timestamp(ts).date().isoformat()
        else:
            calendar_id = self._market_data_subscriptions[symbol].calendar_id
            label = session_label(ts, calendar_id).isoformat()
        if self._adv_session_labels.get(symbol) != label:
            self._adv_session_labels[symbol] = label
            self._adv_filled_quantities[symbol] = 0.0

    def _record_adv_fill(self, symbol: str, quantity: float, executed_at: datetime) -> None:
        """Accumulate confirmed broker fills against the active session budget."""
        if self._adv_lookback_sessions is None:
            return
        if self._interval_delta.days < 1 or symbol not in self._adv_session_labels:
            self._reset_adv_session(symbol, executed_at)
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
            audit_bars[symbol] = audit_history.iloc[-1].to_dict()
            close = float(raw_bar.get("close", float("nan")))
            if not isfinite(close) or close <= 0:
                raise ValueError(f"{symbol} has invalid close at {ts}: {close}")
            raw_bars[symbol] = raw_bar
            if len(history) > 1:
                previous_volume = history.iloc[-2].get("volume")
                if previous_volume is not None and not pd.isna(previous_volume):
                    previous_volumes[symbol] = float(previous_volume)
            self._last_prices[symbol] = close
            self._reset_adv_session(symbol, ts)
            if self._adv_lookback_sessions is not None:
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
            self._persist_state()
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
        self._record_equity(
            ts, raw_bars, used_bar_quantity_by_symbol=cycle_used_bar_quantity_by_symbol
        )
        if self._halted:
            for symbol, position in self._positions.items():
                if symbol in raw_bars:
                    position.periods_held += 1
            self._persist_state()
            return feature_batch.as_of if feature_batch is not None else None

        if live_rebalance_blocks_decisions:
            for symbol, position in self._positions.items():
                if symbol in raw_bars:
                    position.periods_held += 1
            self._persist_state()
            if self._on_ohlcv:
                for symbol, bar in audit_bars.items():
                    self._on_ohlcv(symbol, self._timeframe, bar, ts)
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
                    self._persist_state()
                    return None

        for symbol, position in self._positions.items():
            if symbol in raw_bars:
                position.periods_held += 1
        self._persist_state()

        # Record OHLCV after processing (equity already recorded in Step 1.5)
        if self._on_ohlcv:
            for symbol, bar in audit_bars.items():
                self._on_ohlcv(symbol, self._timeframe, bar, ts)
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

    def _record_equity(
        self,
        ts: datetime,
        bars: dict[str, dict[str, float]],
        *,
        used_bar_quantity_by_symbol: dict[str, float] | None = None,
    ) -> None:
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
                drawdown,
                bars,
                used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
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
        self,
        ts: datetime,
        drawdown: float,
        bars: dict[str, dict[str, float]],
        *,
        used_bar_quantity_by_symbol: dict[str, float] | None = None,
    ) -> None:
        """Queue or submit exits and halt the run's account."""
        exit_queued = False
        if self._executor.simulation:
            queue_market_exit_all(
                self._positions,
                reason=REASON_DRAWDOWN_BREACH,
            )
            flattened = not self._positions
            exit_queued = not flattened
        else:
            actions = [
                OrderIntent(action="close", symbol=symbol, reason=REASON_DRAWDOWN_BREACH)
                for symbol in self._positions
            ]
            reference_bars = {
                symbol: {"close": self._get_last_price(symbol, position)}
                for symbol, position in self._positions.items()
            }
            # Live emergency exits submit the full remaining quantity. Broker
            # execution reports remain authoritative for partial fills.
            flattened = self._execute_live_decision(
                actions,
                reference_bars,
                ts,
                apply_volume_limit=False,
            )
        self._halted = True
        self._pending_decision = []
        self._pending_resting_since = {}
        self._persist_state()
        if flattened:
            outcome = "flattened account positions"
        elif exit_queued:
            outcome = "market exits queued for next observed opens"
        elif self._active_orders:
            outcome = "flattening in progress"
        else:
            outcome = "flatten attempt failed"
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Max Drawdown Breach",
            message=(
                f"account_id={self._account_id} drawdown={drawdown:.2%} <= "
                f"-{self._risk_policy.max_drawdown_rate:.2%} — {outcome} and halted"
            ),
        )
        logger.warning(
            "LiveTrader account %s halted at %s: drawdown %.2f%% breached "
            "max_drawdown_rate=%.2f%% — %s",
            self._account_id,
            ts,
            drawdown * 100,
            self._risk_policy.max_drawdown_rate * 100,
            outcome,
        )
