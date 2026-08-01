"""Polling engine for shadow simulation and broker-confirmed live execution.

Processes newly completed bars as data-driven events and routes intents to
LiveExecutor. Infrastructure integrations are constructor-injected; deployment
factories belong outside the engine.
"""

from __future__ import annotations

import logging
import signal
import types
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from threading import Event
from time import perf_counter
from typing import TYPE_CHECKING, Literal

import pandas as pd

from librae.core import EPSILON
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    ExecutionResult,
    apply_execution_fill,
    calc_equity,
    calculate_position_weights,
    check_stop_targets,
    execute_order_intents,
    execute_pending_decision_and_stops,
    execute_portfolio_targets,
    merge_pending_decisions,
    partition_pending_decision,
    queue_market_exit_all,
    validate_exposure_transition,
    validate_strategy_decision,
)
from librae.core.funding import calculate_funding_cash_flows
from librae.core.liquidity import calculate_lagged_adv
from librae.core.market_data import validate_ohlcv_values
from librae.core.strategy import (
    AccountSnapshot,
    Context,
    Fill,
    MultiLegOrder,
    OrderIntent,
    PortfolioTargets,
    Position,
    PositionState,
    Strategy,
    StrategyDecision,
)
from librae.core.trading_calendar import session_label, session_labels, validate_calendar_id

from .executor import ExecutionReport, LiveExecutor, OrderRequest
from .interfaces import (
    BarCallback,
    BarDataFetcher,
    FundingCashFlowCallback,
    HeartbeatCallback,
    Notifier,
    OhlcvCallback,
    OrderEventCallback,
    PerformanceCallback,
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
    from librae.core.run_config import RunConfig

logger = logging.getLogger(__name__)


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


def _bind_market_data_source(
    source: object,
    instrument: SymbolInfo,
) -> BarDataFetcher:
    """Bind a callable fetcher or a concrete adapter to one resolved symbol."""
    fetch_ohlcv = getattr(source, "fetch_ohlcv", None)
    if not callable(fetch_ohlcv):
        if callable(source):
            return source
        raise TypeError(
            "adapter must be a bar-data callable or expose a callable fetch_ohlcv method"
        )

    if instrument.data_adapter == "ibkr":
        return lambda _symbol, tf, limit, *, drop_incomplete=False: fetch_ohlcv(
            instrument.venue_symbol,
            tf,
            limit=limit,
            security_type=instrument.security_type,
            exchange=instrument.exchange,
            currency=instrument.currency,
            continuous_alias=instrument.continuous_alias,
            contract_month=instrument.contract_month,
            drop_incomplete=drop_incomplete,
        )
    if instrument.data_adapter == "shioaji":
        return lambda _symbol, tf, limit, *, drop_incomplete=False: fetch_ohlcv(
            instrument.venue_symbol,
            tf,
            limit=limit,
            calendar_id=instrument.calendar_id,
            continuous_alias=instrument.continuous_alias,
            contract_month=instrument.contract_month,
            drop_incomplete=drop_incomplete,
        )
    return lambda _symbol, tf, limit, *, drop_incomplete=False: fetch_ohlcv(
        instrument.venue_symbol,
        tf,
        limit=limit,
        continuous_alias=instrument.continuous_alias,
        contract_month=instrument.contract_month,
        drop_incomplete=drop_incomplete,
    )


class LiveTrader:
    """Polling-based runner for sim/live modes.

    Args:
        strategy: Strategy instance (same as backtest).
        feature_fn: Callable(h1_base: DataFrame) -> DataFrame with entry_signal/exit_signal.
        config: RunConfig — the sole configuration source.
        adapter: Callable bar fetcher, concrete adapter with ``fetch_ohlcv``,
            or per-symbol mapping. Required. Extra point-in-time columns reach
            ``feature_fn``.
        order_adapter: Required broker gateway in live mode; unused in sim.
        cost_model: CostModel override. None resolves one model per symbol.
        callbacks: Optional analytics hooks. They have no default persistence
            implementation inside the engine.
        warmup_fetcher: Optional data-layer warmup hook; otherwise the injected
            market-data adapter is used.
        state_store: Optional checkpoint store. Live mode requires a durable
            store so placement attempts and fills survive process restarts.
        runtime_revision: Caller-owned opaque runtime identity. Live mode
            requires it so checkpoints cannot cross code or image revisions.
        notifier: Optional operational notifier implementing ``Notifier``.
        status_interval_periods: Optional polling-period cadence for status
            notifications. Scheduling is separate from the transport.
        on_ready: Optional deployment hook called after state restoration,
            durable ownership, and startup broker reconciliation.
        on_run_registered: Optional hook called with the resolved run_id
            before the first durable checkpoint write, for a caller whose
            state_store enforces a run must be registered first (e.g. a
            foreign key to a run-metadata table). No-op for a restored run,
            since that run is already registered.
    """

    def __init__(
        self,
        strategy: Strategy,
        feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
        *,
        config: RunConfig,
        adapter: object | Mapping[str, object] | None = None,
        order_adapter: object | Mapping[str, object] | None = None,
        cost_model: CostModel | Mapping[str, CostModel] | None = None,
        notifier: Notifier | None = None,
        status_interval_periods: int | None = None,
        on_bar: BarCallback | None = None,
        on_order_event: OrderEventCallback | None = None,
        on_ohlcv: OhlcvCallback | None = None,
        on_heartbeat: HeartbeatCallback | None = None,
        on_signal_outcome: SignalOutcomeCallback | None = None,
        on_funding_cash_flow: FundingCashFlowCallback | None = None,
        on_performance: PerformanceCallback | None = None,
        on_ready: Callable[[str], None] | None = None,
        on_run_registered: Callable[[str], None] | None = None,
        warmup_fetcher: WarmupFetcher | None = None,
        state_store: LiveStateStore | None = None,
        runtime_revision: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        from librae.config.symbols import resolve_symbol
        from librae.core.cost_model import CostModel
        from librae.core.utils import generate_run_id, interval_to_timedelta, to_ccxt

        self._strategy = strategy
        self._feature_fn = feature_fn
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
        if config.execution.adv_lookback_sessions is not None and self._interval_delta.days < 1:
            missing_calendars = sorted(
                symbol
                for symbol, instrument in self._instruments.items()
                if instrument.calendar_id is None
            )
            if missing_calendars:
                raise ValueError(
                    "intraday ADV requires calendar_id for every symbol; missing "
                    f"{missing_calendars}"
                )
            for instrument in self._instruments.values():
                validate_calendar_id(instrument.calendar_id)
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
        self._fetchers = {
            symbol: _bind_market_data_source(sources[symbol], self._instruments[symbol])
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

        # --- Build run_id ---
        strategy_name = config.strategy_name
        self._run_id = generate_run_id(
            f"{strategy_name}_{config.market}",
            config.symbol,
            config.timeframe,
        )

        # --- Build executor ---
        is_live = config.mode == "live"
        self._executor = LiveExecutor(
            resolved_cost_models,
            simulation=not is_live,
            strategy_name=strategy_name,
            order_adapter=order_adapters if is_live else None,
            instruments=self._instruments,
        )

        # --- Restore restart-critical state before callbacks capture run_id ---
        self._state_key = f"{config.mode}:{config.config_hash}"
        self._account_lease_key = f"live-account:{self._account_id}"
        self._runtime_revision = normalize_runtime_revision(
            runtime_revision,
            required=is_live,
        )
        self._state_store = state_store
        if is_live and self._state_store is None:
            raise ValueError("live mode requires an explicit durable state_store")

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._consecutive_errors: int = 0
        self._last_cycle_ts: datetime | None = None
        self._last_bar_ts: dict[str, datetime] = {}
        self._last_funding_ts: dict[str, datetime] = {}
        self._stale_alerted: dict[str, bool] = {}
        self._last_prices: dict[str, float] = {}
        self._positions: dict[str, PositionState] = {}
        self._cash = config.account.initial_cash
        self._halted: bool = False
        self._pending_decision: StrategyDecision = []
        self._active_orders: list[TrackedOrder] = []
        self._live_rebalance: LiveRebalance | None = None
        self._equity_peak = self._cash
        self._prev_equity = self._cash
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
        self._lease_acquired = False
        self._account_lease_acquired = False
        self._restored_state = False
        if self._state_store is not None:
            restored = self._state_store.load(self._state_key)
            if restored is not None:
                self._restore_state(restored)

        configured_warmup = config.execution.warmup_periods
        adv_warmup = (config.execution.adv_lookback_sessions or 0) + 1
        self._warmup_periods = max(configured_warmup, adv_warmup)

        self._on_bar = on_bar
        self._on_order_event = on_order_event
        self._on_ohlcv = on_ohlcv
        self._on_heartbeat = on_heartbeat
        self._on_signal_outcome = on_signal_outcome
        self._on_funding_cash_flow = on_funding_cash_flow
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
        if self._state_store is not None and not self._restored_state:
            if on_run_registered is not None:
                on_run_registered(self._run_id)
            self._persist_state()

    # --- Durable runtime state ---

    def _cash_for_symbol(self, symbol: str) -> float:
        return self._cash

    def _without_halted_account(self, decision: StrategyDecision) -> StrategyDecision:
        return [] if self._halted else decision

    def _snapshot_state(self) -> LiveRuntimeState:
        return LiveRuntimeState(
            state_key=self._state_key,
            run_id=self._run_id,
            config_hash=self._config.config_hash,
            mode=self._config.mode,
            account_id=self._account_id,
            runtime_revision=self._runtime_revision,
            cash=self._cash,
            positions=deepcopy(self._positions),
            last_prices=dict(self._last_prices),
            last_cycle_ts=self._last_cycle_ts,
            last_bar_ts=dict(self._last_bar_ts),
            last_funding_ts=dict(self._last_funding_ts),
            pending_decision=deepcopy(self._pending_decision),
            active_orders=deepcopy(self._active_orders),
            live_rebalance=deepcopy(self._live_rebalance),
            equity_peak=self._equity_peak,
            prev_equity=self._prev_equity,
            trade_count=self._trade_count,
            event_sequence=self._event_sequence,
            period_index=self._period_index,
            status_period_count=self._status_period_count,
            halted=self._halted,
            adv_session_labels=dict(self._adv_session_labels),
            adv_filled_quantities=dict(self._adv_filled_quantities),
        )

    def _restore_state(self, state: LiveRuntimeState) -> None:
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
        self._cash = state.cash
        self._positions = state.positions
        self._last_prices = state.last_prices
        self._last_cycle_ts = state.last_cycle_ts
        self._last_bar_ts = state.last_bar_ts
        self._last_funding_ts = state.last_funding_ts
        self._pending_decision = state.pending_decision
        self._active_orders = state.active_orders
        self._live_rebalance = state.live_rebalance
        self._equity_peak = state.equity_peak
        self._prev_equity = state.prev_equity
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

    def _persist_state(self, *orders: TrackedOrder) -> None:
        """Critical checkpoint write; failures propagate and stop the cycle."""
        if self._state_store is not None:
            self._state_store.save(self._snapshot_state(), orders)

    # WHY: 3 consecutive errors likely means a persistent issue (API down, DB
    # unreachable), not a transient blip — worth alerting the operator.
    CONSECUTIVE_ERROR_THRESHOLD = 3

    # WHY: a completed bar's own timestamp is always ~1 interval behind wall
    # clock even when the feed is perfectly healthy (see _check_staleness) —
    # this is how many *additional* full intervals of no progress are
    # tolerated on top of that before alerting. Live skips stale frames rather
    # than submitting decisions from obsolete market snapshots.
    STALE_DATA_TOLERANCE_BARS = 2

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
        """Alert if the latest fetched bar hasn't advanced in wall-clock
        time — catches a feed that stops updating without ever raising an
        exception (CONSECUTIVE_ERROR_THRESHOLD only covers raised errors).
        Edge-triggered: alerts once when crossing into stale, not every
        poll cycle, and re-arms once fresh data resumes. Returns whether the
        frame is stale so live execution can fail closed.
        """
        age = self._utc_now() - latest_ts
        threshold = (self.STALE_DATA_TOLERANCE_BARS + 1) * self._interval_delta
        is_stale = age > threshold
        was_stale = self._stale_alerted.get(symbol, False)

        if is_stale and not was_stale:
            self._stale_alerted[symbol] = True
            logger.warning(
                "Stale data: %s latest bar age=%s (threshold=%s)", symbol, age, threshold
            )
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Stale Data: {symbol}",
                message=f"Latest bar is {age} old (threshold {threshold}) — feed may have stopped updating.",
            )
        elif not is_stale and was_stale:
            self._stale_alerted[symbol] = False
            logger.info("Stale data recovered: %s", symbol)
        return is_stale

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

    def _fetch_runtime_frames(self) -> dict[str, pd.DataFrame]:
        """Fetch configured symbols with explicit bounded concurrency."""

        def fetch_one(symbol: str) -> tuple[pd.DataFrame | None, float]:
            started = perf_counter()
            frame = self._fetch_with_cache(symbol)
            return frame, perf_counter() - started

        if self._market_data_workers == 1 or len(self._symbols) == 1:
            results = {symbol: fetch_one(symbol) for symbol in self._symbols}
        else:
            worker_count = min(self._market_data_workers, len(self._symbols))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {symbol: pool.submit(fetch_one, symbol) for symbol in self._symbols}
                results = {symbol: futures[symbol].result() for symbol in self._symbols}

        frames: dict[str, pd.DataFrame] = {}
        for symbol, (frame, elapsed) in results.items():
            self._cycle_fetch_seconds[symbol] = elapsed
            if frame is not None:
                frames[symbol] = frame
        return frames

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
        known_clients = {order.request.client_order_id for order in self._active_orders}
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
                    f"another live process already owns account_id={self._account_id!r}"
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
            if self._live_rebalance is not None and not self._active_orders and not self._halted:
                self._queue_next_live_rebalance_order()
                self._advance_live_orders()

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
        if self._on_heartbeat:
            self._on_heartbeat(self._run_id)

        frames: dict[str, pd.DataFrame] = {}
        for symbol, df in self._fetch_runtime_frames().items():
            if df is None or df.empty:
                continue

            latest = pd.Timestamp(df["ts"].iloc[-1])
            if latest.tzinfo is None:
                raise ValueError(f"{symbol} latest completed bar timestamp must be timezone-aware")
            latest_ts = latest.to_pydatetime().astimezone(UTC)
            is_stale = self._check_staleness(symbol, latest_ts)
            if is_stale and not self._executor.simulation:
                logger.warning("Skipping stale live frame for %s at %s", symbol, latest_ts)
                continue
            frames[symbol] = df

        # Keep heartbeat, cache, and staleness monitoring alive while a broker
        # order is resting. Strategy evaluation remains serialized behind the
        # active order so a later bar cannot create a conflicting order queue.
        if not self._executor.simulation and (self._active_orders or self._halted):
            return

        pending_timestamps: set[datetime] = set()
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
                pending_timestamps.add(timestamp.to_pydatetime().astimezone(UTC))

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

        for cycle_ts in sorted(pending_timestamps):
            if self._last_cycle_ts is not None and cycle_ts < self._last_cycle_ts:
                raise RuntimeError(
                    "out-of-order completed bar cannot be applied after a newer event: "
                    f"{cycle_ts} < {self._last_cycle_ts}"
                )

            event_frames: dict[str, pd.DataFrame] = {}
            advanced_symbols: list[str] = []
            for symbol, frame in frames.items():
                normalized = pd.to_datetime(frame["ts"], utc=True)
                if not bool((normalized == pd.Timestamp(cycle_ts)).any()):
                    continue
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
            self._process_cycle(event_frames, cycle_ts)

            # Commit watermarks only after the event was processed successfully.
            for symbol in advanced_symbols:
                self._last_bar_ts[symbol] = cycle_ts
            self._last_cycle_ts = cycle_ts
            self._persist_state()

    def _fetch_with_cache(self, symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV and keep a sorted, deduplicated rolling cache."""
        try:
            if symbol not in self._ohlcv_cache:
                if self._warmup_fetcher:
                    new_df = self._warmup_fetcher(symbol, self._timeframe, self._warmup_periods)
                else:
                    new_df = self._fetchers[symbol](
                        symbol,
                        self._timeframe,
                        self._warmup_periods,
                        drop_incomplete=True,
                    )
                cached = None
            else:
                cached = self._ohlcv_cache[symbol]
                new_df = self._fetchers[symbol](
                    symbol,
                    self._timeframe,
                    2,
                    drop_incomplete=True,
                )

            if new_df.empty:
                return cached if cached is not None else new_df

            merged = new_df if cached is None else pd.concat([cached, new_df], ignore_index=True)
            merged = merged.drop_duplicates(subset="ts", keep="last").sort_values("ts")
            if cached is not None:
                merged = merged.iloc[-self._warmup_periods :]
            merged = merged.reset_index(drop=True)
            validate_ohlcv_values(merged, context=f"{symbol} runtime data")
            self._ohlcv_cache[symbol] = merged
            return merged
        except Exception:
            logger.exception("Failed to fetch %s", symbol)
            return self._ohlcv_cache.get(symbol)

    def _publish_action_results(self, result: ExecutionResult) -> None:
        """Publish notifications and analytics after state is committed."""
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
            if self._on_order_event:
                self._on_order_event(event, self._event_sequence)

            if event.event_type in ("open", "add"):
                label = (
                    event.side.upper()
                    if event.event_type == "open"
                    else f"{event.side.upper()} ADD"
                )
                logger.info("SIGNAL %s %s @ %.2f", label, event.symbol, event.price)
                self._notify(
                    "send_signal",
                    strategy=self._executor.strategy_name,
                    symbol=event.symbol,
                    side=label,
                    price=event.price,
                )

        for trade in result.trades:
            logger.info("SIGNAL EXIT %s @ %.2f", trade.symbol, trade.exit_price)
            self._notify(
                "send_signal",
                strategy=self._executor.strategy_name,
                symbol=trade.symbol,
                side="EXIT",
                price=trade.exit_price,
            )
            logger.info("Position closed: %s @ %.2f", trade.symbol, trade.exit_price)

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
        """Apply venue normalization, then enforce the live limit-price collar."""
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

        def get_price(symbol: str, _action: OrderIntent) -> float | None:
            return prices.get(symbol)

        def get_volume(symbol: str) -> float | None:
            volume = bars.get(symbol, {}).get("volume")
            return float(volume) if volume is not None else None

        max_order_notional = (
            self._risk_policy.max_order_notional if apply_entry_risk_limits else None
        )
        volume_limit = self._max_bar_volume_participation_rate if apply_volume_limit else None
        adv_limit = self._max_adv_participation_rate if apply_volume_limit else None
        staged_positions = deepcopy(self._positions)
        planned_bar_quantity_by_symbol = dict(used_bar_quantity_by_symbol or {})
        planned_adv_quantity_by_symbol = dict(self._adv_filled_quantities)
        lagged_adv = lagged_adv_by_symbol or {}

        if isinstance(intent, PortfolioTargets):
            max_position_notional = (
                self._risk_policy.max_position_weight * self._prev_equity
                if apply_entry_risk_limits and self._risk_policy.max_position_weight
                else None
            )
            result = execute_portfolio_targets(
                intent,
                staged_positions,
                self._cash,
                ts,
                get_price=get_price,
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
            )
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
                    self._prepare_live_order(
                        request,
                        reference_price=prices[event.symbol],
                    )
                )
            return requests

        actions = list(intent.legs) if isinstance(intent, MultiLegOrder) else intent
        requests: list[OrderRequest] = []
        planning_cash = self._cash
        planning_exposure_prices = dict(exposure_prices)
        for action in actions:
            if action.stop_price is not None or action.take_profit_price is not None:
                raise ValueError(
                    "Live stop-loss/take-profit requires broker-native protective orders; "
                    "completed-bar range checks are simulation-only"
                )
            order_type = "limit" if action.limit_price is not None else "market"
            limit_price = action.limit_price
            planning_action = replace(action, limit_price=None)
            symbol = action.symbol or primary_symbol
            positions_before_action = deepcopy(staged_positions)
            cash_before_action = planning_cash
            max_position_notional = (
                self._risk_policy.max_position_weight * self._prev_equity
                if apply_entry_risk_limits and self._risk_policy.max_position_weight
                else None
            )
            result = execute_order_intents(
                [planning_action],
                staged_positions,
                planning_cash,
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
                get_lagged_adv=lambda symbol: lagged_adv.get(symbol),
                used_bar_quantity_by_symbol=planned_bar_quantity_by_symbol,
                used_adv_quantity_by_symbol=planned_adv_quantity_by_symbol,
            )
            planning_cash += result.cash_delta
            planning_exposure_prices.update({event.symbol: event.price for event in result.events})
            if apply_entry_risk_limits:
                validate_exposure_transition(
                    positions_before=positions_before_action,
                    cash_before=cash_before_action,
                    positions_after=staged_positions,
                    cash_after=planning_cash,
                    prices=planning_exposure_prices,
                    get_cost_model=self._get_cost_model,
                    max_gross_exposure=self._risk_policy.max_gross_exposure,
                    max_net_exposure=self._risk_policy.max_net_exposure,
                )
            sequence_offset = sequence_start + len(requests)
            for index, event in enumerate(result.events):
                request = self._executor.request_from_event(
                    event,
                    order_type=order_type,
                    limit_price=limit_price,
                    sequence=sequence_offset + index,
                )
                requests.append(
                    self._prepare_live_order(
                        request,
                        reference_price=prices[event.symbol],
                    )
                )
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
        if isinstance(intent, MultiLegOrder):
            self._halt_live(
                title="Unsupported Live Multi-leg Decision",
                message=(
                    "generic serial execution cannot guarantee a related-order group; "
                    "use a venue-native combo adapter or a strategy-owned coordinator"
                ),
            )
            return False

        if isinstance(intent, PortfolioTargets):
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
            if not self._queue_next_live_rebalance_order():
                return not self._halted
            self._advance_live_orders()
            return self._live_rebalance is None and not self._active_orders and not self._halted

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

    def _queue_next_live_rebalance_order(self) -> bool:
        """Recalculate and queue one remaining target leg from confirmed state."""
        batch = self._live_rebalance
        if batch is None:
            return False
        if self._active_orders:
            raise RuntimeError("cannot replan live targets while an order is active")
        bars = {
            symbol: {
                "close": price,
                "volume": batch.reference_volumes.get(symbol),
            }
            for symbol, price in batch.reference_prices.items()
        }
        try:
            requests = self._plan_live_orders(
                batch.targets,
                bars,
                batch.decided_at,
                lagged_adv_by_symbol=batch.lagged_adv_by_symbol,
                used_bar_quantity_by_symbol=batch.filled_bar_quantity_by_symbol,
                sequence_start=batch.next_sequence,
            )
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
            or self._active_orders[0].status == "cancel_pending"
        ):
            tracked = self._active_orders[0]
            request = tracked.request
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
                        self._halt_live(
                            title="Ambiguous Order Placement",
                            message=(
                                f"{request.symbol} qty={request.quantity:.4f} client_order_id="
                                f"{request.client_order_id} was not found after placement failure"
                            ),
                        )
                        return
            elif tracked.order_id:
                report = self._timed_order_call(
                    lambda request=request, order_id=tracked.order_id: self._executor.get_order(
                        request, order_id
                    )
                )
            else:
                report = self._timed_order_call(
                    lambda request=request: self._executor.find_order(request)
                )
                if report is None:
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
                self._halt_live(
                    title=f"Order {report.status.title()}",
                    message=(
                        f"{request.symbol} order_id={report.order_id or 'unassigned'} "
                        f"filled={report.filled_quantity:.4f}/"
                        f"{report.requested_quantity:.4f}"
                    ),
                )
                return
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
            if (
                not self._active_orders
                and self._live_rebalance is not None
                and self._queue_next_live_rebalance_order()
            ):
                continue

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
        latest_report: ExecutionReport,
    ) -> None:
        """Cancel one stale live order, preserving any cumulative broker fill."""
        request = tracked.request
        order_id = latest_report.order_id or tracked.order_id
        if not order_id:
            self._halt_live(
                title="Order Timeout Unresolved",
                message=(
                    f"{request.symbol} client_order_id={request.client_order_id} "
                    "exceeded its local timeout without a broker order id"
                ),
            )
            return
        try:
            cancel_report = self._timed_order_call(
                lambda: self._executor.cancel_order(request, order_id)
            )
            if cancel_report is None:
                raise RuntimeError("broker cancellation returned no execution report")
            self._apply_order_report(tracked, cancel_report)
        except Exception as exc:
            self._halt_live(
                title="Order Timeout Cancellation Failed",
                message=(
                    f"{request.symbol} order_id={order_id} could not be confirmed cancelled: {exc}"
                ),
            )
            return

        if cancel_report.status == "filled":
            return
        status = cancel_report.status
        if status not in ("cancelled", "rejected"):
            tracked.status = "cancel_pending"
            self._persist_state(tracked)
        self._halt_live(
            title=(
                "Order Timeout"
                if status in ("cancelled", "rejected")
                else "Order Timeout Cancellation Unresolved"
            ),
            message=(
                f"{request.symbol} order_id={order_id} status={status} "
                f"filled={cancel_report.filled_quantity:.4f}/"
                f"{cancel_report.requested_quantity:.4f}"
            ),
        )

    def _apply_order_report(
        self,
        tracked: TrackedOrder,
        report: ExecutionReport,
    ) -> None:
        """Apply only the new cumulative-fill delta, then checkpoint it."""
        request = tracked.request
        if report.symbol != request.symbol or report.side != request.side:
            raise ValueError("broker report identity does not match the tracked request")
        if tracked.order_id and report.order_id != tracked.order_id:
            raise ValueError("broker order id changed during its lifecycle")
        if report.filled_quantity + EPSILON < tracked.filled_quantity:
            raise ValueError("broker cumulative filled quantity moved backwards")
        if report.requested_quantity > request.quantity + EPSILON:
            raise ValueError("broker requested quantity exceeds the tracked request")

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

    def _cancel_active_orders(self) -> None:
        """Best-effort cancellation used whenever live trading halts."""
        for tracked in list(self._active_orders):
            try:
                if not tracked.placement_attempted:
                    tracked.status = "cancelled"
                    self._active_orders.remove(tracked)
                    self._persist_state(tracked)
                    continue
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
                    continue
                cancellation_was_pending = tracked.status == "cancel_pending"
                if (
                    report.status not in ("filled", "cancelled", "rejected", "cancel_pending")
                    and not cancellation_was_pending
                ):
                    report = self._timed_order_call(
                        lambda request=tracked.request, order_id=report.order_id: (
                            self._executor.cancel_order(
                                request,
                                order_id,
                            )
                        )
                    )
                    if report is None:
                        raise RuntimeError("broker cancellation returned no execution report")
                self._apply_order_report(tracked, report)
                if report.status not in ("filled", "cancelled", "rejected"):
                    tracked.status = "cancel_pending"
                    self._persist_state(tracked)
            except Exception:
                logger.exception(
                    "Failed to cancel tracked order %s",
                    tracked.request.client_order_id,
                )

    def _process_bar(self, symbol: str, raw_df: pd.DataFrame, ts: datetime) -> None:
        """Process one symbol through the portfolio-cycle path."""
        self._process_cycle({symbol: raw_df}, ts)

    def _reset_adv_session(self, symbol: str, ts: datetime) -> None:
        """Reset one symbol's cumulative ADV usage when its session changes."""
        if self._adv_lookback_sessions is None:
            return
        if self._interval_delta.days >= 1:
            label = pd.Timestamp(ts).date().isoformat()
        else:
            calendar_id = self._instruments[symbol].calendar_id
            if calendar_id is None:  # guarded during construction
                raise RuntimeError(f"missing calendar_id for {symbol}")
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
    ) -> None:
        """Execute and evaluate one data-driven market event.

        Simulation fills previous-cycle intent on this completed raw bar.
        Live mode never books that historical range: it evaluates the
        completed bar, submits the current decision immediately, and commits
        only broker-confirmed execution reports.
        """
        primary_symbol = self._symbols[0]
        histories: dict[str, pd.DataFrame] = {}
        raw_bars: dict[str, dict[str, float]] = {}
        lagged_adv_by_symbol: dict[str, float] = {}
        for symbol, raw_df in raw_frames.items():
            history = raw_df[raw_df["ts"] <= ts].set_index("ts")
            history.index.name = "ts"
            if history.empty or pd.Timestamp(history.index[-1]).to_pydatetime() != ts:
                continue
            histories[symbol] = history
            raw_bar = history.iloc[-1].to_dict()
            close = float(raw_bar.get("close", float("nan")))
            if not isfinite(close) or close <= 0:
                raise ValueError(f"{symbol} has invalid close at {ts}: {close}")
            raw_bars[symbol] = raw_bar
            self._last_prices[symbol] = close
            self._reset_adv_session(symbol, ts)
            if self._adv_lookback_sessions is not None:
                calendar_id = self._instruments[symbol].calendar_id
                labels = None
                if self._interval_delta.days < 1:
                    if calendar_id is None:  # guarded during construction
                        raise RuntimeError(f"missing calendar_id for {symbol}")
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

        self._pending_decision = self._without_halted_account(self._pending_decision)
        ready_decision, waiting_decision = partition_pending_decision(
            self._pending_decision,
            raw_bars,
            self._positions,
            primary_symbol=primary_symbol,
        )
        self._pending_decision = waiting_decision
        cycle_used_bar_quantity_by_symbol: dict[str, float] = {}
        if self._executor.simulation:
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
                    get_lagged_adv=lambda symbol: lagged_adv_by_symbol.get(symbol),
                    used_bar_quantity_by_symbol=cycle_used_bar_quantity_by_symbol,
                    used_adv_quantity_by_symbol=self._adv_filled_quantities,
                )
                staged_cash = self._cash + step_result.cash_delta
            else:
                max_position_notional = (
                    self._risk_policy.max_position_weight * self._prev_equity
                    if self._risk_policy.max_position_weight
                    else None
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
                    get_lagged_adv=lambda symbol: lagged_adv_by_symbol.get(symbol),
                    used_adv_quantity_by_symbol=self._adv_filled_quantities,
                    max_gross_exposure=self._risk_policy.max_gross_exposure,
                    max_net_exposure=self._risk_policy.max_net_exposure,
                    exposure_prices=exposure_prices,
                )
            self._commit_simulated_results(
                cash=staged_cash,
                positions=self._positions,
                result=step_result,
            )
            self._apply_funding_cash_flows(ts, raw_bars)
            for event in step_result.events:
                cycle_used_bar_quantity_by_symbol[event.symbol] = (
                    cycle_used_bar_quantity_by_symbol.get(event.symbol, 0.0) + event.fill_quantity
                )
        elif ready_decision and not self._execute_live_decision(
            ready_decision,
            raw_bars,
            ts,
            lagged_adv_by_symbol=lagged_adv_by_symbol,
        ):
            self._persist_state()
            return

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
            return

        bars: dict[str, dict[str, float]] = {}
        for symbol, history in histories.items():
            try:
                featured = self._feature_fn(history)
            except Exception:
                logger.exception(
                    "Feature computation failed for %s; cycle %s remains uncommitted",
                    symbol,
                    ts,
                )
                # Execution of an already-pending order is independent of
                # feature computation. Persist that fill, but leave the market
                # data watermark unchanged so the decision phase is retried.
                self._persist_state()
                raise

            bar = featured.iloc[-1].to_dict()
            bars[symbol] = bar
            price = float(bar.get("close", float("nan")))
            if not isfinite(price) or price <= 0:
                raise ValueError(f"{symbol} feature output has invalid close at {ts}: {price}")
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

        # A target-weight basket waits for a complete synchronous snapshot;
        # per-symbol intents do not prevent decisions for other symbols.
        if not isinstance(self._pending_decision, (PortfolioTargets, MultiLegOrder)):
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
            )
            intent = self._without_halted_account(intent)
            self._period_index += 1
            if self._executor.simulation:
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
                    self._execute_live_decision(
                        ready_decision,
                        raw_bars,
                        ts,
                        lagged_adv_by_symbol=lagged_adv_by_symbol,
                    )

        for symbol, position in self._positions.items():
            if symbol in raw_bars:
                position.periods_held += 1
        self._persist_state()

        # Record OHLCV after processing (equity already recorded in Step 1.5)
        if self._on_ohlcv:
            for symbol, bar in raw_bars.items():
                self._on_ohlcv(symbol, self._timeframe, bar, ts)

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

    def _apply_funding_cash_flows(
        self,
        ts: datetime,
        bars: Mapping[str, Mapping[str, object]],
    ) -> None:
        """Apply each simulation funding observation at most once."""
        eligible_bars = {
            symbol: bar
            for symbol, bar in bars.items()
            if ts
            > self._last_funding_ts.get(
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
        for symbol in observed_symbols:
            self._last_funding_ts[symbol] = ts
        for cash_flow in cash_flows:
            self._cash += cash_flow.cash_flow
            if self._on_funding_cash_flow:
                self._on_funding_cash_flow(cash_flow)
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
                    daily_pnl=period_return * equity,
                    position=pos_str,
                )

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
