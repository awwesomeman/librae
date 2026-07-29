"""LiveTrader — polling loop for sim and live modes.

Processes newly completed bars as data-driven events and routes intents to
LiveExecutor. Caches OHLCV to avoid redundant fetches.

Wiring is internalized: pass config=RunConfig to __init__,
the engine builds adapter, cost_model, callbacks, telegram internally.
Use on_bar=None etc. to disable specific callbacks (e.g. in tests).
"""

from __future__ import annotations

import logging
import signal
import time
import types
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from time import perf_counter
from typing import TYPE_CHECKING, Literal

import pandas as pd

from librae.core import EPSILON
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    ExecutionResult,
    apply_execution_fill,
    calc_equity,
    check_stop_targets,
    execute_order_intents,
    execute_pending_decision_and_stops,
    execute_portfolio_targets,
    merge_pending_decisions,
    partition_pending_decision,
    queue_market_exit_all,
    side_multiplier,
    validate_strategy_decision,
)
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
from .state import (
    LiveMultiLeg,
    LiveRebalance,
    LiveRuntimeState,
    LiveStateStore,
    TrackedOrder,
)

if TYPE_CHECKING:
    from librae.core.cost_model import CostModel
    from librae.core.executor import OrderEvent, TradeResult
    from librae.core.run_config import RunConfig

logger = logging.getLogger(__name__)

OHLCVFetcher = Callable[..., pd.DataFrame]

_UNSET = object()  # sentinel: distinguish "not passed" from "explicitly passed None"
_REASON_MULTI_LEG_RESTORE = "multi_leg_restore"
_DATA_ADAPTER_BY_BROKER = {
    "binance": "crypto",
    "ibkr": "ibkr",
    "shioaji": "shioaji",
}


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


def _build_builtin_adapter(name: str, *, trading: bool) -> object:
    """Construct one explicitly selected built-in adapter."""
    if name == "shioaji":
        from brokers.shioaji_adapter import ShioajiAdapter

        return ShioajiAdapter()
    if name == "ibkr":
        from brokers.ibkr_adapter import IBKRAdapter

        return IBKRAdapter(trading_enabled=trading)
    if name in ("crypto", "binance"):
        from brokers.crypto_adapter import CryptoAdapter, CryptoCredentials

        credentials = (
            CryptoCredentials.from_env("BINANCE", exchange_id="binance") if trading else None
        )
        return CryptoAdapter(credentials=credentials)
    raise ValueError(f"Unsupported adapter: {name!r}")


class LiveTrader:
    """Polling-based runner for sim/live modes.

    Args:
        strategy: Strategy instance (same as backtest).
        feature_fn: Callable(h1_base: DataFrame) -> DataFrame with entry_signal/exit_signal.
        config: RunConfig — the sole configuration source.
        adapter: Market-data fetcher override. None builds the data adapter
            from each symbol's explicit data_source route.
        order_adapter: Order gateway override. In live mode, omitting it
            requires an explicit config.broker or per-symbol broker route;
            execution is never inferred from the symbol or market.
        cost_model: CostModel override. None resolves one model per symbol.
        on_bar: _UNSET -> build DB callback from config; None -> no callback; callable -> use it.
        on_order_event: Same pattern as on_bar.
        on_ohlcv: Same pattern as on_bar.
        on_heartbeat: Same pattern as on_bar.
        on_signal_outcome: Same pattern as on_bar.
        warmup_fetcher: _UNSET or None -> plain API fetch via adapter; callable ->
            use it (e.g. a DB-first fetcher supplied by the caller's data layer).
        state_store: _UNSET -> TimescaleDB when DB is enabled; explicit
            duck-typed store -> use it. Live mode requires a store so
            placement attempts and fills survive process restarts.
        notifier: _UNSET -> build default TelegramAdapter from config (skipped
            entirely when config.no_db); None -> no notifications; object -> use it
            (must implement TelegramAdapter's duck-typed interface).
    """

    def __init__(
        self,
        strategy: Strategy,
        feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
        *,
        config: RunConfig,
        adapter: OHLCVFetcher | Mapping[str, OHLCVFetcher] | None = None,
        order_adapter: object | Mapping[str, object] | None = None,
        cost_model: CostModel | Mapping[str, CostModel] | None = None,
        notifier: object | None = _UNSET,
        on_bar: Callable[..., None] | object | None = _UNSET,
        on_order_event: Callable[..., None] | object | None = _UNSET,
        on_ohlcv: Callable[..., None] | object | None = _UNSET,
        on_heartbeat: Callable[..., None] | object | None = _UNSET,
        on_signal_outcome: Callable[..., None] | object | None = _UNSET,
        warmup_fetcher: Callable[..., pd.DataFrame] | object | None = _UNSET,
        state_store: LiveStateStore | object | None = _UNSET,
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
        self._poll_seconds = config.poll_seconds
        self._reconciliation_interval_seconds = config.reconciliation_interval_seconds
        self._market_data_workers = config.market_data_workers
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
        if any(instrument.account_id is None for instrument in self._instruments.values()):
            raise RuntimeError("resolved instruments must have an account_id")
        self._account_id_by_symbol = {
            symbol: str(instrument.account_id) for symbol, instrument in self._instruments.items()
        }
        self._currency_by_account = {
            account_id: account.currency for account_id, account in config.accounts.items()
        }

        # --- Build per-symbol market-data adapters ---
        data_adapters: dict[str, object] = {}
        if adapter is not None:
            if isinstance(adapter, Mapping):
                missing = set(self._symbols) - set(adapter)
                if missing:
                    raise ValueError(f"Missing market-data adapters for symbols: {sorted(missing)}")
                self._fetchers = dict(adapter)
            else:
                self._fetchers = {symbol: adapter for symbol in self._symbols}
        else:
            adapter_instances: dict[tuple[str, str], object] = {}
            self._fetchers: dict[str, OHLCVFetcher] = {}
            for symbol, instrument in self._instruments.items():
                key = (instrument.data_adapter, instrument.data_source)
                instance = adapter_instances.get(key)
                if instance is None:
                    if instrument.data_adapter == "crypto" and instrument.continuous_alias:
                        raise ValueError(
                            f"{symbol!r} is a continuous crypto alias and is not directly "
                            "orderable; inject a market-data adapter and configure a concrete "
                            "venue_symbol before using sim/live"
                        )
                    route = (config.instrument_overrides or {}).get(symbol, {})
                    broker = route.get("broker") or config.broker
                    instance = _build_builtin_adapter(
                        instrument.data_adapter,
                        trading=(
                            config.mode == "live"
                            and order_adapter is None
                            and _DATA_ADAPTER_BY_BROKER.get(broker) == instrument.data_adapter
                        ),
                    )
                adapter_instances[key] = instance
                data_adapters[symbol] = instance

                if instrument.data_adapter == "ibkr":
                    self._fetchers[symbol] = (
                        lambda _symbol, tf, limit, *, drop_incomplete=False, _adapter=instance, _instrument=instrument: (
                            _adapter.fetch_ohlcv(
                                _instrument.venue_symbol,
                                tf,
                                limit=limit,
                                security_type=_instrument.security_type,
                                exchange=_instrument.exchange,
                                currency=_instrument.currency,
                                drop_incomplete=drop_incomplete,
                            )
                        )
                    )
                elif instrument.data_adapter == "shioaji":
                    self._fetchers[symbol] = (
                        lambda _symbol, tf, limit, *, drop_incomplete=False, _adapter=instance, _instrument=instrument: (
                            _adapter.fetch_ohlcv(
                                _instrument.venue_symbol,
                                tf,
                                limit=limit,
                                calendar_id=_instrument.calendar_id,
                                drop_incomplete=drop_incomplete,
                            )
                        )
                    )
                else:
                    self._fetchers[symbol] = (
                        lambda _symbol, tf, limit, *, drop_incomplete=False, _adapter=instance, _instrument=instrument: (
                            _adapter.fetch_ohlcv(
                                _instrument.venue_symbol,
                                tf,
                                limit=limit,
                                drop_incomplete=drop_incomplete,
                            )
                        )
                    )

        if config.mode == "live":
            if isinstance(order_adapter, Mapping):
                order_adapters = dict(order_adapter)
            elif order_adapter is not None:
                order_adapters = {symbol: order_adapter for symbol in self._symbols}
            else:
                broker_instances: dict[str, object] = {}
                order_adapters = {}
                for symbol, instrument in self._instruments.items():
                    route = (config.instrument_overrides or {}).get(symbol, {})
                    broker = route.get("broker") or config.broker
                    if not broker:
                        raise ValueError(
                            f"Live execution broker is not configured for {symbol!r}; "
                            "set strategy.broker/instrument_overrides or inject order_adapter"
                        )
                    broker_data_adapter = _DATA_ADAPTER_BY_BROKER.get(broker)
                    if broker_data_adapter is None:
                        raise ValueError(f"Unsupported execution broker: {broker!r}")
                    instance = broker_instances.get(broker)
                    if instance is None:
                        data_instance = data_adapters.get(symbol)
                        instance = (
                            data_instance
                            if broker_data_adapter == instrument.data_adapter
                            and data_instance is not None
                            else _build_builtin_adapter(broker, trading=True)
                        )
                        broker_instances[broker] = instance
                    order_adapters[symbol] = instance
            missing = set(self._symbols) - set(order_adapters)
            if missing:
                raise ValueError(f"Missing order adapters for symbols: {sorted(missing)}")
            adapters_by_account: dict[str, set[int]] = {}
            accounts_by_adapter: dict[int, set[str]] = {}
            for symbol, order_route in order_adapters.items():
                account_id = self._account_id_by_symbol[symbol]
                adapter_identity = id(order_route)
                adapters_by_account.setdefault(account_id, set()).add(adapter_identity)
                accounts_by_adapter.setdefault(adapter_identity, set()).add(account_id)
            split_accounts = sorted(
                account_id
                for account_id, adapter_ids in adapters_by_account.items()
                if len(adapter_ids) != 1
            )
            shared_adapters = [
                sorted(account_ids)
                for account_ids in accounts_by_adapter.values()
                if len(account_ids) != 1
            ]
            if split_accounts or shared_adapters:
                raise ValueError(
                    "live account_id must map one-to-one to an order adapter; "
                    f"split_accounts={split_accounts}, shared_adapters={shared_adapters}"
                )
        else:
            order_adapters = {}

        # --- Resolve notifier (Telegram by default; injectable) ---
        # config.no_db gates this the same way it gates the db callbacks below
        # (dry_run implies no_db — see RunConfig.__post_init__), so a fully
        # local run never imports the notifications package.
        if notifier is not _UNSET:
            resolved_notifier = notifier
        elif config.no_db:
            resolved_notifier = None
        else:
            resolved_notifier = self._build_notifier()

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
            telegram=resolved_notifier,
            strategy_name=strategy_name,
            order_adapter=order_adapters if is_live else None,
            instruments=self._instruments,
        )

        # --- Restore restart-critical state before callbacks capture run_id ---
        self._state_key = f"{config.mode}:{config.config_hash}"
        if state_store is not _UNSET:
            self._state_store = state_store
        elif config.no_db:
            self._state_store = None
        else:
            from db.timescale_state import TimescaleLiveStateStore

            self._state_store = TimescaleLiveStateStore()
        if is_live and self._state_store is None:
            raise ValueError(
                "Live mode requires durable state; enable DB or pass state_store explicitly"
            )

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._consecutive_errors: int = 0
        self._db_write_failures: int = 0
        self._last_cycle_ts: datetime | None = None
        self._last_bar_ts: dict[str, datetime] = {}
        self._stale_alerted: dict[str, bool] = {}
        self._last_prices: dict[str, float] = {}
        self._positions: dict[str, PositionState] = {}
        self._cash_by_account = {
            account_id: account.initial_cash for account_id, account in config.accounts.items()
        }
        self._halted: bool = False
        self._halted_accounts: set[str] = set()
        self._pending_decision: StrategyDecision = []
        self._active_orders: list[TrackedOrder] = []
        self._live_rebalance: LiveRebalance | None = None
        self._live_multi_leg: LiveMultiLeg | None = None
        self._equity_peak_by_account = dict(self._cash_by_account)
        self._prev_equity_by_account = dict(self._cash_by_account)
        self._trade_count: int = 0
        self._event_sequence: int = 0
        self._performance_dirty: bool = False
        self._pending_traded_notional_by_account = dict.fromkeys(
            self._cash_by_account,
            0.0,
        )
        self._portfolio_diagnostics_by_account = {
            account_id: (0.0, 0.0, 0.0, 0.0) for account_id in self._cash_by_account
        }
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
        self._restored_state = False
        if self._state_store is not None:
            restored = self._state_store.load(self._state_key)
            if restored is not None:
                self._restore_state(restored)

        # --- Resolve callbacks (sentinel pattern) ---
        configured_warmup = (config.params or {}).get("warmup_periods", 720)
        adv_warmup = (config.execution.adv_lookback_sessions or 0) + 1
        self._warmup_periods = max(configured_warmup, adv_warmup)

        callbacks = {
            "on_bar": (on_bar, self._build_on_bar),
            "on_order_event": (on_order_event, self._build_on_order_event),
            "on_ohlcv": (on_ohlcv, self._build_on_ohlcv),
            "on_heartbeat": (on_heartbeat, self._build_on_heartbeat),
            "on_signal_outcome": (on_signal_outcome, self._build_on_signal_outcome),
            "warmup_fetcher": (warmup_fetcher, self._build_warmup_fetcher),
        }
        for attr, (value, builder) in callbacks.items():
            if value is not _UNSET:
                setattr(self, f"_{attr}", value)
            elif config.no_db:
                setattr(self, f"_{attr}", None)
            else:
                setattr(self, f"_{attr}", builder())

        if not config.no_db:
            self._register_run()

        self._fill_price = config.execution.default_fill_price
        self._max_bar_volume_participation_rate = config.execution.max_bar_volume_participation_rate
        self._adv_lookback_sessions = config.execution.adv_lookback_sessions
        self._max_adv_participation_rate = config.execution.max_adv_participation_rate
        self._risk_policy = config.risk
        self._running: bool = False

        # Cache status config
        tg_obj = self._executor.telegram
        self._status_enabled: bool = bool(tg_obj and tg_obj.notifications.status.enabled)
        self._status_interval: int = tg_obj.notifications.status.interval_periods if tg_obj else 0

        self._notify_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg")
        self._sleep = time.sleep  # instance attribute so tests can skip real delays
        if self._state_store is not None and not self._restored_state:
            self._persist_state()

    # --- Durable runtime state ---

    def _cash_for_symbol(self, symbol: str) -> float:
        return self._cash_by_account[self._account_id_by_symbol[symbol]]

    def _account_positions(self, account_id: str) -> dict[str, PositionState]:
        return {
            symbol: position
            for symbol, position in self._positions.items()
            if self._account_id_by_symbol[symbol] == account_id
        }

    def _replace_account_positions(
        self,
        account_id: str,
        account_positions: dict[str, PositionState],
    ) -> None:
        for symbol in list(self._positions):
            if self._account_id_by_symbol[symbol] == account_id:
                del self._positions[symbol]
        self._positions.update(account_positions)

    def _decisions_by_account(
        self,
        decision: StrategyDecision,
        *,
        primary_symbol: str,
    ) -> dict[str, StrategyDecision]:
        if isinstance(decision, PortfolioTargets):
            account_ids = {self._account_id_by_symbol[symbol] for symbol in decision.weights}
            if not account_ids:
                account_ids = {self._account_id_by_symbol[primary_symbol]}
            if len(account_ids) != 1:
                raise ValueError(
                    "PortfolioTargets cannot span isolated accounts; emit explicit "
                    "OrderIntent quantities or MultiLegOrder instead"
                )
            return {next(iter(account_ids)): decision}
        actions = list(decision.legs) if isinstance(decision, MultiLegOrder) else list(decision)
        grouped: dict[str, list[OrderIntent]] = {}
        for action in actions:
            symbol = action.symbol or primary_symbol
            grouped.setdefault(self._account_id_by_symbol[symbol], []).append(action)
        return grouped

    def _without_halted_accounts(
        self,
        decision: StrategyDecision,
        *,
        primary_symbol: str,
    ) -> StrategyDecision:
        """Discard exposure decisions owned by drawdown-halted accounts."""
        if not decision or not self._halted_accounts:
            return decision
        if isinstance(decision, PortfolioTargets):
            account_ids = {self._account_id_by_symbol[symbol] for symbol in decision.weights} or {
                self._account_id_by_symbol[primary_symbol]
            }
            return [] if account_ids & self._halted_accounts else decision
        if isinstance(decision, MultiLegOrder):
            account_ids = {
                self._account_id_by_symbol[leg.symbol or primary_symbol] for leg in decision.legs
            }
            return [] if account_ids & self._halted_accounts else decision
        return [
            action
            for action in decision
            if self._account_id_by_symbol[action.symbol or primary_symbol]
            not in self._halted_accounts
        ]

    def _snapshot_state(self) -> LiveRuntimeState:
        return LiveRuntimeState(
            state_key=self._state_key,
            run_id=self._run_id,
            config_hash=self._config.config_hash,
            mode=self._config.mode,
            cash_by_account=dict(self._cash_by_account),
            positions=deepcopy(self._positions),
            last_prices=dict(self._last_prices),
            last_cycle_ts=self._last_cycle_ts,
            last_bar_ts=dict(self._last_bar_ts),
            pending_decision=deepcopy(self._pending_decision),
            active_orders=deepcopy(self._active_orders),
            live_rebalance=deepcopy(self._live_rebalance),
            live_multi_leg=deepcopy(self._live_multi_leg),
            equity_peak_by_account=dict(self._equity_peak_by_account),
            prev_equity_by_account=dict(self._prev_equity_by_account),
            trade_count=self._trade_count,
            event_sequence=self._event_sequence,
            period_index=self._period_index,
            status_period_count=self._status_period_count,
            halted=self._halted,
            halted_accounts=set(self._halted_accounts),
            adv_session_labels=dict(self._adv_session_labels),
            adv_filled_quantities=dict(self._adv_filled_quantities),
        )

    def _restore_state(self, state: LiveRuntimeState) -> None:
        if state.state_key != self._state_key:
            raise ValueError("runtime state key does not match this configuration")
        if state.config_hash != self._config.config_hash or state.mode != self._config.mode:
            raise ValueError("runtime state configuration does not match this run")
        self._run_id = state.run_id
        if set(state.cash_by_account) != set(self._cash_by_account):
            raise ValueError("runtime state accounts do not match this run")
        self._cash_by_account = state.cash_by_account
        self._positions = state.positions
        self._last_prices = state.last_prices
        self._last_cycle_ts = state.last_cycle_ts
        self._last_bar_ts = state.last_bar_ts
        self._pending_decision = state.pending_decision
        self._active_orders = state.active_orders
        self._live_rebalance = state.live_rebalance
        self._live_multi_leg = state.live_multi_leg
        self._equity_peak_by_account = state.equity_peak_by_account
        self._prev_equity_by_account = state.prev_equity_by_account
        self._trade_count = state.trade_count
        self._event_sequence = state.event_sequence
        self._period_index = state.period_index
        self._status_period_count = state.status_period_count
        self._halted = state.halted
        self._halted_accounts = set(state.halted_accounts)
        self._adv_session_labels = state.adv_session_labels
        self._adv_filled_quantities = state.adv_filled_quantities
        self._restored_state = True
        logger.info(
            "Restored runtime state: key=%s run_id=%s cycle=%s orders=%d "
            "halted=%s halted_accounts=%s",
            self._state_key,
            self._run_id,
            self._last_cycle_ts,
            len(self._active_orders),
            self._halted,
            sorted(self._halted_accounts),
        )

    def _persist_state(self, *orders: TrackedOrder) -> None:
        """Critical checkpoint write; failures propagate and stop the cycle."""
        if self._state_store is not None:
            self._state_store.save(self._snapshot_state(), orders)

    # --- Callback builders ---

    def _db_write(self, fn: Callable[..., object], *args: object, **kwargs: object) -> None:
        """Best-effort analytics write; runtime checkpoints use _persist_state.

        Analytics failures do not interrupt trading, but sustained failures
        alert. Checkpoint failures propagate because trading without durable
        state would make restart behavior unsafe.
        """
        try:
            fn(*args, **kwargs)
            self._db_write_failures = 0
        except Exception as e:
            self._db_write_failures += 1
            logger.warning(
                "DB %s failed (%d consecutive): %s", fn.__name__, self._db_write_failures, e
            )
            if self._db_write_failures == self.CONSECUTIVE_ERROR_THRESHOLD:
                self._notify(
                    "send_alert",
                    title=f"[{self._executor.strategy_name}] DB Write Failing",
                    message=(
                        f"{self._db_write_failures} consecutive DB write failures "
                        f"(last: {fn.__name__}). Check DB connectivity — trading continues."
                    ),
                )

    def _register_run(self) -> None:
        from db.timescale_writer import write_run_metadata

        try:
            write_run_metadata(
                run_id=self._run_id,
                strategy=self._config.strategy_name,
                symbols=self._config.symbols,
                timeframe=self._config.timeframe,
                mode=self._config.mode,
                started_at=datetime.now(tz=UTC),
                data_source=self._config.data_source,
                poll_seconds=self._config.poll_seconds,
                params=self._config.params,
                execution_policy=asdict(self._config.execution),
                risk_policy=asdict(self._config.risk),
                perf_params=self._config.perf_params,
                # config_hash intentionally omitted: idx_backtest_runs_config_hash is a
                # unique index meant for backtest dedup (check_existing_run) -- sim/live
                # runs legitimately restart with an identical config (e.g. after a
                # crash) and must never collide on it.
            )
        except Exception as e:
            logger.warning("DB write_run_metadata failed: %s", e)

    def _build_on_bar(self) -> Callable:
        from db.timescale_writer import write_equity_curve_point

        strategy = self._config.strategy_name

        def on_bar(
            run_id_: str,
            ts: datetime,
            account_id: str,
            currency: str,
            equity: float,
            drawdown: float,
            period_return: float,
        ) -> None:
            gross, net, concentration, turnover = self._portfolio_diagnostics_by_account[account_id]
            self._db_write(
                write_equity_curve_point,
                ts=ts,
                run_id=run_id_,
                account_id=account_id,
                currency=currency,
                equity=equity,
                drawdown=drawdown,
                period_return=period_return,
                gross_exposure=gross,
                net_exposure=net,
                concentration=concentration,
                turnover=turnover,
                exposed=gross > EPSILON,
                strategy=strategy,
            )

        return on_bar

    def _build_on_order_event(self) -> Callable:
        from db.timescale_writer import write_trade_event

        from librae.core.utils import make_event_id

        run_id = self._run_id
        strategy = self._config.strategy_name
        timeframe = self._config.timeframe
        config = self._config

        def on_order_event(event: OrderEvent) -> None:
            self._event_sequence += 1
            fields = asdict(event)
            fields["event_id"] = make_event_id(run_id, self._event_sequence)
            fields["run_id"] = run_id
            fields["strategy"] = strategy
            fields["mode"] = config.mode
            fields["timeframe"] = timeframe
            account_id = self._account_id_by_symbol[event.symbol]
            fields["account_id"] = account_id
            fields["currency"] = self._currency_by_account[account_id]
            self._db_write(write_trade_event, **fields)
            if not self._executor.simulation:
                self._persist_state()
            if event.event_type in ("close", "reduce"):
                self._performance_dirty = True

        return on_order_event

    def _build_on_ohlcv(self) -> Callable:
        from db.timescale_writer import write_ohlcv

        def on_ohlcv(symbol: str, timeframe_: str, bar: dict[str, float], ts: datetime) -> None:
            row = pd.DataFrame(
                [
                    {
                        "ts": ts,
                        "open": bar["open"],
                        "high": bar["high"],
                        "low": bar["low"],
                        "close": bar["close"],
                        "volume": bar["volume"],
                    }
                ]
            ).set_index("ts")
            self._db_write(
                write_ohlcv,
                row,
                symbol,
                timeframe_,
                data_source=self._instruments[symbol].data_source,
            )

        return on_ohlcv

    def _build_on_heartbeat(self) -> Callable:
        from db.timescale_writer import update_heartbeat

        def on_heartbeat(run_id_: str) -> None:
            self._db_write(update_heartbeat, run_id_)

        return on_heartbeat

    def _build_on_signal_outcome(self) -> Callable:
        from db.timescale_writer import write_signal_event

        run_id = self._run_id
        strategy = self._config.strategy_name
        timeframe = self._config.timeframe

        def on_signal_event_cb(
            symbol: str,
            ts: datetime,
            signal_value: float,
            price: float,
            signal_type: str = "entry",
        ) -> None:
            self._db_write(
                write_signal_event,
                ts=ts,
                run_id=run_id,
                strategy=strategy,
                symbol=symbol,
                mode=self._config.mode,
                timeframe=timeframe,
                signal_value=signal_value,
                price=price,
                signal_type=signal_type,
            )

        return on_signal_event_cb

    def _build_notifier(self) -> object | None:
        """Default notifier: TelegramAdapter built from config.telegram_config
        + TELEGRAM_* env vars. Lazy import so a fully local run (config.no_db,
        or an explicit notifier= override) never touches the notifications
        package."""
        from notifications.config import TelegramConfig
        from notifications.telegram import TelegramAdapter, TelegramCredentials

        tg_config = TelegramConfig.from_dict(self._config.telegram_config or {})
        tg_creds = TelegramCredentials.from_env("TELEGRAM")
        return TelegramAdapter(config=tg_config, credentials=tg_creds)

    def _build_warmup_fetcher(self) -> Callable:
        """Default warmup: plain API fetch via the symbol's adapter — librae has no
        data-access layer of its own to warm up from. A DB-first fetcher
        (read cached history, gap-fill from API) needs a data-access layer
        that lives outside librae; pass warmup_fetcher= explicitly for that."""

        def warmup_fetcher(symbol: str, tf_ccxt: str, limit: int) -> pd.DataFrame:
            return self._fetchers[symbol](
                symbol,
                tf_ccxt,
                limit,
                drop_incomplete=True,
            )

        return warmup_fetcher

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
        """Submit a Telegram notification to the background thread pool."""
        telegram = self._executor.telegram
        if not telegram:
            return
        fn = getattr(telegram, method)
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
            adapter = self._executor.get_order_adapter(symbol)
            instrument = self._instruments[symbol]
            broker_pos = adapter.get_position(instrument.venue_symbol)
            size = float(broker_pos.get("size") or 0)
            if not size:
                continue
            raw_average = broker_pos.get("avg_price")
            avg_price = float(raw_average) if raw_average is not None else None
            if avg_price is not None and avg_price <= 0:
                raise ValueError(f"broker returned invalid average price for {symbol}")
            side: Literal["long", "short"] = "long" if size > 0 else "short"
            positions[symbol] = _BrokerPosition(
                side=side,
                quantity=abs(size),
                average_price=avg_price,
            )
        return positions

    @staticmethod
    def _position_books_match(
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

        Duck-typed and best-effort: adapters without a get_balance() method
        are silently skipped, as is a market this engine has no settlement
        currency mapping for — nothing to compare against either way.
        """
        if self._executor.simulation:
            return

        for account_id, local_cash in self._cash_by_account.items():
            symbol = next(
                symbol
                for symbol in self._symbols
                if self._account_id_by_symbol[symbol] == account_id
            )
            adapter = self._executor.get_order_adapter(symbol)
            if not callable(getattr(adapter, "get_balance", None)):
                continue
            currency = self._currency_by_account[account_id]
            try:
                broker_total = float(adapter.get_balance(currency)["total"])
            except Exception:
                logger.exception(
                    "Cash reconciliation failed for account=%s currency=%s; skipping",
                    account_id,
                    currency,
                )
                continue
            if not isfinite(broker_total):
                logger.warning(
                    "Cash reconciliation returned non-finite total for account=%s; skipping",
                    account_id,
                )
                continue
            drift_pct = abs(broker_total - local_cash) / max(local_cash, EPSILON)
            if drift_pct <= self.CASH_RECONCILE_TOLERANCE_PCT:
                continue
            logger.warning(
                "Cash drift: account=%s local=%.2f broker=%.2f (%s), not auto-adjusted",
                account_id,
                local_cash,
                broker_total,
                currency,
            )
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Cash Reconciliation Drift",
                message=(
                    f"account_id={account_id} local_cash={local_cash:.2f} "
                    f"broker_balance={broker_total:.2f} ({currency}) "
                    f"drift={drift_pct:.2%}, review manually"
                ),
            )
        return

    def _maybe_reconcile_runtime(self) -> None:
        """Periodically compare broker facts without mutating the local ledger."""
        if (
            self._executor.simulation
            or self._halted
            or self._active_orders
            or self._live_rebalance is not None
            or self._live_multi_leg is not None
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
        if self._live_multi_leg is not None and self._live_multi_leg.phase == "executing":
            self._live_multi_leg.phase = "restoring"
        if not self._executor.simulation:
            self._cancel_active_orders()
            if self._live_multi_leg is not None and not self._active_orders:
                self._queue_multi_leg_restore()
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
            (
                tracked.request.reason == REASON_DRAWDOWN_BREACH
                and tracked.request.position_effect in ("reduce", "close")
            )
            or self._is_multi_leg_restore_order(tracked.request)
            for tracked in self._active_orders
        )

    def _is_multi_leg_restore_order(self, request: OrderRequest) -> bool:
        """Identify engine recovery without trusting a user-controlled reason alone."""
        return bool(
            self._live_multi_leg is not None
            and self._live_multi_leg.phase == "restoring"
            and request.reason == _REASON_MULTI_LEG_RESTORE
        )

    def _initialize_run(self) -> None:
        """Acquire live ownership and reconcile broker facts before polling."""
        if not self._executor.simulation:
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
            elif self._live_multi_leg is not None and not self._active_orders:
                if self._live_multi_leg.phase == "restoring":
                    self._queue_multi_leg_restore()
                elif not self._halted:
                    self._queue_next_live_multi_leg_order()
                self._advance_live_orders()

    def _release_lease(self) -> None:
        if self._lease_acquired:
            self._state_store.release_lease(self._state_key)
            self._lease_acquired = False

    def run(self, max_iterations: int | None = None) -> None:
        """Start the polling loop. Blocks until stopped or max_iterations reached."""
        self._running = True
        self._setup_signal_handlers()
        try:
            self._initialize_run()
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

    @property
    def last_cycle_diagnostics(self) -> CycleDiagnostics | None:
        """Return measured latency for the latest poll cycle."""
        return self._last_cycle_diagnostics

    def halt(self, reason: str = "operator requested halt") -> None:
        """Fail closed immediately until an operator calls ``reset_halt``."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("halt reason must be a non-empty string")
        self._halt_live(title="Manual Halt", message=reason.strip())

    def reset_halt(self, account_id: str | None = None) -> None:
        """Start a new global or account-specific risk epoch after review."""
        if self._active_orders:
            raise RuntimeError("cannot reset halt while broker orders remain unresolved")
        if self._live_multi_leg is not None:
            raise RuntimeError(
                "cannot reset halt while multi-leg exposure requires manual reconciliation"
            )
        account_snapshots = self._calc_account_snapshots()
        if account_id is not None:
            if account_id not in self._cash_by_account:
                raise ValueError(f"unknown account_id: {account_id!r}")
            self._halted_accounts.discard(account_id)
            equity = account_snapshots[account_id][0]
            self._equity_peak_by_account[account_id] = equity
            self._prev_equity_by_account[account_id] = equity
        else:
            self._halted = False
            self._halted_accounts.clear()
            self._equity_peak_by_account = {
                current_account_id: equity
                for current_account_id, (equity, _) in account_snapshots.items()
            }
            self._prev_equity_by_account = dict(self._equity_peak_by_account)
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
            account_id = self._account_id_by_symbol[event.symbol]
            self._pending_traded_notional_by_account[account_id] += abs(event.notional)
            logger.info(
                "Order event: %s %s %s %.4f @ %.2f",
                event.event_type,
                event.side,
                event.symbol,
                event.fill_quantity,
                event.price,
            )
            if self._on_order_event:
                self._on_order_event(event)

            if event.event_type in ("open", "add"):
                self._executor.notify_entry(event.symbol, event.side, event.price, event.event_type)

        for trade in result.trades:
            self._executor.notify_exit(trade.symbol, trade.exit_price)
            logger.info("Position closed: %s @ %.2f", trade.symbol, trade.exit_price)

    def _commit_simulated_results(
        self,
        *,
        cash_by_account: dict[str, float],
        positions: dict[str, PositionState],
        result: ExecutionResult,
    ) -> None:
        """Commit a deterministic simulated fill batch."""
        self._cash_by_account = cash_by_account
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
            if intent.fill_price is not None:
                raise ValueError(
                    "Live PortfolioTargets.fill_price is unsupported; "
                    "target rebalances submit market orders after the completed-bar decision"
                )
            account_ids = {self._account_id_by_symbol[symbol] for symbol in intent.weights}
            if not account_ids:
                account_ids = {self._account_id_by_symbol[primary_symbol]}
            if len(account_ids) != 1:
                raise ValueError(
                    "PortfolioTargets cannot span isolated accounts; emit explicit "
                    "OrderIntent quantities or MultiLegOrder instead"
                )
            account_id = next(iter(account_ids))
            account_positions = {
                symbol: position
                for symbol, position in staged_positions.items()
                if self._account_id_by_symbol[symbol] == account_id
            }
            max_position_notional = (
                self._risk_policy.max_position_weight * self._prev_equity_by_account[account_id]
                if apply_entry_risk_limits and self._risk_policy.max_position_weight
                else None
            )
            result = execute_portfolio_targets(
                intent,
                account_positions,
                self._cash_by_account[account_id],
                ts,
                get_price=get_price,
                get_cost_model=self._get_cost_model,
                primary_symbol=primary_symbol,
                max_position_notional=max_position_notional,
                max_order_notional=max_order_notional,
                max_bar_volume_participation_rate=volume_limit,
                max_adv_participation_rate=adv_limit,
                max_gross_exposure=self._risk_policy.max_gross_exposure,
                max_net_exposure=self._risk_policy.max_net_exposure,
                get_volume=get_volume,
                get_lagged_adv=lambda symbol: lagged_adv.get(symbol),
                used_bar_quantity_by_symbol=planned_bar_quantity_by_symbol,
                used_adv_quantity_by_symbol=planned_adv_quantity_by_symbol,
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
        planning_cash_by_account = dict(self._cash_by_account)
        for action in actions:
            if action.stop_price is not None or action.take_profit_price is not None:
                raise ValueError(
                    "Live stop-loss/take-profit requires broker-native protective orders; "
                    "completed-bar range checks are simulation-only"
                )
            if isinstance(action.fill_price, str):
                raise ValueError(
                    "Live OrderIntent.fill_price cannot name a historical bar field; "
                    "use None for market or a numeric price for a broker limit order"
                )

            order_type = "limit" if isinstance(action.fill_price, (int, float)) else "market"
            limit_price = float(action.fill_price) if order_type == "limit" else None
            planning_action = replace(action, fill_price="close")
            symbol = action.symbol or primary_symbol
            account_id = self._account_id_by_symbol[symbol]
            max_position_notional = (
                self._risk_policy.max_position_weight * self._prev_equity_by_account[account_id]
                if apply_entry_risk_limits and self._risk_policy.max_position_weight
                else None
            )
            result = execute_order_intents(
                [planning_action],
                staged_positions,
                planning_cash_by_account[account_id],
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
            planning_cash_by_account[account_id] += result.cash_delta
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
        intent = self._without_halted_accounts(
            intent,
            primary_symbol=self._symbols[0],
        )
        if not intent:
            return True
        if isinstance(intent, MultiLegOrder):
            if self._live_multi_leg is not None:
                raise RuntimeError("cannot replace an active multi-leg order")
            leg_symbols = {leg.symbol for leg in intent.legs}
            self._live_multi_leg = LiveMultiLeg(
                order=intent,
                baseline_signed_quantities={
                    symbol: (
                        self._positions[symbol].quantity
                        * side_multiplier(self._positions[symbol].side)
                        if symbol in self._positions
                        else 0.0
                    )
                    for symbol in leg_symbols
                },
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
            if not self._queue_next_live_multi_leg_order():
                return not self._halted
            self._advance_live_orders()
            return self._live_multi_leg is None and not self._active_orders and not self._halted

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

    def _queue_next_live_multi_leg_order(self) -> bool:
        """Queue one group leg; every later leg waits for a confirmed fill."""
        group = self._live_multi_leg
        if group is None or group.phase != "executing":
            return False
        if self._active_orders:
            raise RuntimeError("cannot queue a multi-leg order while another order is active")
        if group.next_leg_index >= len(group.order.legs):
            violation = self._post_fill_risk_violation()
            if violation is not None:
                self._halt_live(title="Post-fill Risk Breach", message=violation)
                return False
            self._live_multi_leg = None
            self._persist_state()
            return False

        action = group.order.legs[group.next_leg_index]
        if not action.reason and group.order.reason:
            action = replace(action, reason=group.order.reason)
        bars = {
            symbol: {
                "close": price,
                "volume": group.reference_volumes.get(symbol),
            }
            for symbol, price in group.reference_prices.items()
        }
        try:
            requests = self._plan_live_orders(
                [action],
                bars,
                group.decided_at,
                lagged_adv_by_symbol=group.lagged_adv_by_symbol,
                sequence_start=group.next_leg_index,
            )
        except ValueError as exc:
            self._halt_live(title="Multi-leg Preflight Rejected", message=str(exc))
            return False
        if len(requests) != 1:
            self._halt_live(
                title="Multi-leg Preflight Rejected",
                message=(
                    f"leg {group.next_leg_index} for {action.symbol} did not produce "
                    "exactly one broker order"
                ),
            )
            return False

        tracked = TrackedOrder(request=requests[0])
        group.next_leg_index += 1
        self._active_orders.append(tracked)
        self._persist_state(tracked)
        return True

    def _queue_multi_leg_restore(self) -> bool:
        """Queue compensating orders that restore pre-group signed quantities."""
        group = self._live_multi_leg
        if group is None or group.phase != "restoring" or self._active_orders:
            return False
        actions: list[OrderIntent] = []
        for leg in reversed(group.order.legs):
            symbol = leg.symbol
            baseline = group.baseline_signed_quantities[symbol]
            position = self._positions.get(symbol)
            current = (
                position.quantity * side_multiplier(position.side) if position is not None else 0.0
            )
            if abs(current - baseline) <= EPSILON:
                continue
            same_direction = (
                abs(current) <= EPSILON
                or abs(baseline) <= EPSILON
                or (current > 0) == (baseline > 0)
            )
            if same_direction:
                if abs(current) > abs(baseline):
                    actions.append(
                        OrderIntent(
                            action="close",
                            symbol=symbol,
                            quantity=abs(current) - abs(baseline),
                            reason=_REASON_MULTI_LEG_RESTORE,
                        )
                    )
                else:
                    actions.append(
                        OrderIntent(
                            action="long" if baseline > 0 else "short",
                            symbol=symbol,
                            quantity=abs(baseline) - abs(current),
                            reason=_REASON_MULTI_LEG_RESTORE,
                        )
                    )
                continue
            actions.extend(
                (
                    OrderIntent(
                        action="close",
                        symbol=symbol,
                        quantity=abs(current),
                        reason=_REASON_MULTI_LEG_RESTORE,
                    ),
                    OrderIntent(
                        action="long" if baseline > 0 else "short",
                        symbol=symbol,
                        quantity=abs(baseline),
                        reason=_REASON_MULTI_LEG_RESTORE,
                    ),
                )
            )
        if not actions:
            self._live_multi_leg = None
            self._persist_state()
            return False
        leg_symbols = set(group.baseline_signed_quantities)
        bars = {
            symbol: {
                "close": self._last_prices.get(
                    symbol,
                    group.reference_prices[symbol],
                ),
                "volume": None,
            }
            for symbol in leg_symbols
        }
        try:
            requests = self._plan_live_orders(
                actions,
                bars,
                self._utc_now(),
                apply_volume_limit=False,
                apply_entry_risk_limits=False,
            )
        except ValueError as exc:
            logger.exception("Unable to plan multi-leg baseline restoration")
            group.phase = "manual"
            self._persist_state()
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Multi-leg Restore Failed",
                message=f"{exc}; manual broker intervention required.",
            )
            return False
        if not requests:
            group.phase = "manual"
            self._persist_state()
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Multi-leg Restore Failed",
                message="baseline restoration produced no broker orders; "
                "manual broker intervention required.",
            )
            return False
        queued = [TrackedOrder(request=request) for request in requests]
        self._active_orders.extend(queued)
        self._persist_state(*queued)
        return bool(queued)

    def _timed_order_call(
        self,
        callback: Callable[[], ExecutionReport | None],
    ) -> ExecutionReport | None:
        started = perf_counter()
        try:
            return callback()
        finally:
            self._cycle_order_seconds += perf_counter() - started

    def _multi_leg_deadline_missed(self) -> bool:
        group = self._live_multi_leg
        return bool(
            group is not None
            and group.phase == "executing"
            and group.first_fill_at is not None
            and (self._utc_now() - group.first_fill_at).total_seconds()
            >= group.order.max_completion_seconds
        )

    def _halt_for_multi_leg_deadline(self) -> None:
        group = self._live_multi_leg
        if group is None:
            return
        self._halt_live(
            title="Multi-leg Completion Deadline Missed",
            message=(
                "related order group remained incomplete for "
                f"{group.order.max_completion_seconds:.3f}s"
            ),
        )

    def _advance_live_orders(self, *, submit_planned: bool = True) -> None:
        """Poll or submit the head order; dependent orders stay serialized."""
        while self._active_orders and (
            not self._halted
            or self._has_active_recovery_orders()
            or self._active_orders[0].status == "cancel_pending"
        ):
            if self._multi_leg_deadline_missed():
                self._halt_for_multi_leg_deadline()
                return
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
            if self._multi_leg_deadline_missed():
                self._halt_for_multi_leg_deadline()
                return
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
                if self._is_multi_leg_restore_order(request):
                    self._live_multi_leg.phase = "manual"
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
                and not self._is_multi_leg_restore_order(request)
            ):
                risk_violation = self._post_fill_risk_violation(
                    include_net=not (
                        self._live_multi_leg is not None
                        and self._live_multi_leg.phase == "executing"
                    )
                )
                if risk_violation is not None:
                    self._halt_live(
                        title="Post-fill Risk Breach",
                        message=risk_violation,
                    )
                    return
            if report.status != "filled" and self._live_order_timed_out(tracked):
                if self._is_multi_leg_restore_order(request):
                    self._live_multi_leg.phase = "manual"
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
            if not self._active_orders and self._live_multi_leg is not None:
                if self._live_multi_leg.phase == "executing":
                    if self._queue_next_live_multi_leg_order():
                        continue
                elif self._queue_multi_leg_restore():
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
            account_id = self._account_id_by_symbol[report.symbol]
            self._cash_by_account[account_id], result = apply_execution_fill(
                self._positions,
                self._cash_by_account[account_id],
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
            if (
                self._live_multi_leg is not None
                and self._live_multi_leg.phase == "executing"
                and self._live_multi_leg.first_fill_at is None
            ):
                self._live_multi_leg.first_fill_at = self._utc_now()

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

        self._pending_decision = self._without_halted_accounts(
            self._pending_decision,
            primary_symbol=primary_symbol,
        )
        ready_decision, waiting_decision = partition_pending_decision(
            self._pending_decision,
            raw_bars,
            self._positions,
            primary_symbol=primary_symbol,
        )
        self._pending_decision = waiting_decision
        cycle_used_bar_quantity_by_symbol: dict[str, float] = {}
        if self._executor.simulation:
            decisions = self._decisions_by_account(
                ready_decision,
                primary_symbol=primary_symbol,
            )
            staged_cash_by_account = dict(self._cash_by_account)
            step_trades: list[TradeResult] = []
            step_events: list[OrderEvent] = []
            for account_id in staged_cash_by_account:
                account_positions = self._account_positions(account_id)
                if self._halted or account_id in self._halted_accounts:
                    account_result = check_stop_targets(
                        account_positions,
                        raw_bars,
                        ts,
                        get_cost_model=self._get_cost_model,
                        max_bar_volume_participation_rate=(self._max_bar_volume_participation_rate),
                        max_adv_participation_rate=self._max_adv_participation_rate,
                        get_lagged_adv=lambda symbol: lagged_adv_by_symbol.get(symbol),
                        used_bar_quantity_by_symbol=cycle_used_bar_quantity_by_symbol,
                        used_adv_quantity_by_symbol=self._adv_filled_quantities,
                    )
                    staged_cash_by_account[account_id] += account_result.cash_delta
                else:
                    max_position_notional = (
                        self._risk_policy.max_position_weight
                        * self._prev_equity_by_account[account_id]
                        if self._risk_policy.max_position_weight
                        else None
                    )
                    staged_cash, account_result = execute_pending_decision_and_stops(
                        ts,
                        account_positions,
                        staged_cash_by_account[account_id],
                        decisions.get(account_id, []),
                        raw_bars,
                        get_cost_model=self._get_cost_model,
                        default_fill=self._fill_price,
                        primary_symbol=primary_symbol,
                        max_position_notional=max_position_notional,
                        max_order_notional=self._risk_policy.max_order_notional,
                        max_bar_volume_participation_rate=(self._max_bar_volume_participation_rate),
                        max_adv_participation_rate=self._max_adv_participation_rate,
                        get_lagged_adv=lambda symbol: lagged_adv_by_symbol.get(symbol),
                        used_adv_quantity_by_symbol=self._adv_filled_quantities,
                        max_gross_exposure=self._risk_policy.max_gross_exposure,
                        max_net_exposure=self._risk_policy.max_net_exposure,
                    )
                    staged_cash_by_account[account_id] = staged_cash
                self._replace_account_positions(account_id, account_positions)
                step_trades.extend(account_result.trades)
                step_events.extend(account_result.events)
            step_result = ExecutionResult(
                trades=step_trades,
                events=step_events,
                cash_delta=0.0,
            )
            self._commit_simulated_results(
                cash_by_account=staged_cash_by_account,
                positions=self._positions,
                result=step_result,
            )
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
            account_values = self._calc_account_snapshots()
            position_snapshot = {
                symbol: position
                for _, positions in account_values.values()
                for symbol, position in positions.items()
            }
            ctx = Context(
                ts=ts,
                symbol=primary_symbol,
                symbols=self._symbols,
                bar=bars.get(primary_symbol, {}),
                bars=bars,
                positions=position_snapshot,
                accounts={
                    account_id: AccountSnapshot(
                        currency=self._currency_by_account[account_id],
                        cash=self._cash_by_account[account_id],
                        equity=equity,
                    )
                    for account_id, (equity, _) in account_values.items()
                },
                account_id_by_symbol=self._account_id_by_symbol,
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
            intent = self._without_halted_accounts(
                intent,
                primary_symbol=primary_symbol,
            )
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
        for account_id, (equity, _) in self._calc_account_snapshots().items():
            account_positions = self._account_positions(account_id)
            if not account_positions:
                continue
            if equity <= EPSILON:
                return f"account {account_id} confirmed equity is non-positive ({equity:.6f})"
            signed_weights: list[tuple[str, float]] = []
            for symbol, position in account_positions.items():
                notional = (
                    position.quantity
                    * self._get_last_price(symbol, position)
                    * self._get_cost_model(symbol).multiplier
                )
                signed_weight = notional / equity
                if position.side == "short":
                    signed_weight = -signed_weight
                signed_weights.append((symbol, signed_weight))

            max_position_weight = self._risk_policy.max_position_weight
            if max_position_weight is not None:
                for symbol, weight in signed_weights:
                    if abs(weight) > max_position_weight + EPSILON:
                        return (
                            f"{symbol} confirmed weight {abs(weight):.6f} exceeds "
                            f"max_position_weight={max_position_weight:.6f}"
                        )
            gross = sum(abs(weight) for _, weight in signed_weights)
            max_gross = self._risk_policy.max_gross_exposure
            if max_gross is not None and gross > max_gross + EPSILON:
                return (
                    f"account {account_id} confirmed gross exposure {gross:.6f} "
                    f"exceeds max_gross_exposure={max_gross:.6f}"
                )
            if include_net:
                net = abs(sum(weight for _, weight in signed_weights))
                max_net = self._risk_policy.max_net_exposure
                if max_net is not None and net > max_net + EPSILON:
                    return (
                        f"account {account_id} confirmed absolute net exposure "
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

    def _calc_account_snapshots(
        self,
    ) -> dict[str, tuple[float, dict[str, Position]]]:
        """Mark each isolated account independently."""
        return {
            account_id: calc_equity(
                self._cash_by_account[account_id],
                self._account_positions(account_id),
                get_price=self._get_last_price,
                get_cost_model=self._get_cost_model,
            )
            for account_id in self._cash_by_account
        }

    def _record_equity(
        self,
        ts: datetime,
        bars: dict[str, dict[str, float]],
        *,
        used_bar_quantity_by_symbol: dict[str, float] | None = None,
    ) -> None:
        """Calculate equity + drawdown, check the max-drawdown circuit
        breaker, call on_bar callback, and send periodic status."""
        snapshots = self._calc_account_snapshots()
        breached_accounts: list[tuple[str, float]] = []
        for account_id, (equity, _) in snapshots.items():
            peak = max(self._equity_peak_by_account[account_id], equity)
            self._equity_peak_by_account[account_id] = peak
            drawdown = (equity - peak) / peak if peak > 0 else 0.0
            if (
                self._risk_policy.max_drawdown_rate
                and not self._halted
                and account_id not in self._halted_accounts
                and drawdown <= -self._risk_policy.max_drawdown_rate
            ):
                breached_accounts.append((account_id, drawdown))
        if breached_accounts:
            self._flatten_accounts_and_halt(
                ts,
                breached_accounts,
                bars,
                used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
            )
            snapshots = self._calc_account_snapshots()

        status_values: list[tuple[str, float, float, float]] = []
        for account_id, (equity, _) in snapshots.items():
            previous_equity = self._prev_equity_by_account[account_id]
            peak = self._equity_peak_by_account[account_id]
            drawdown = (equity - peak) / peak if peak > 0 else 0.0
            period_return = equity / previous_equity - 1.0 if previous_equity > 0 else 0.0
            self._prev_equity_by_account[account_id] = equity
            signed_weights: list[float] = []
            if equity > EPSILON:
                for symbol, position in self._account_positions(account_id).items():
                    notional = (
                        position.quantity
                        * self._get_last_price(symbol, position)
                        * self._get_cost_model(symbol).multiplier
                    )
                    signed_weights.append(
                        notional / equity * (1.0 if position.side == "long" else -1.0)
                    )
            traded_notional = self._pending_traded_notional_by_account[account_id]
            self._portfolio_diagnostics_by_account[account_id] = (
                sum(abs(weight) for weight in signed_weights),
                sum(signed_weights),
                max((abs(weight) for weight in signed_weights), default=0.0),
                traded_notional / equity if equity > EPSILON else 0.0,
            )
            self._pending_traded_notional_by_account[account_id] = 0.0
            if self._on_bar:
                self._on_bar(
                    self._run_id,
                    ts,
                    account_id,
                    self._currency_by_account[account_id],
                    equity,
                    drawdown,
                    period_return,
                )
            status_values.append((account_id, equity, drawdown, period_return))
        if self._performance_dirty:
            from db.timescale_writer import refresh_performance

            # The current equity point must exist before KPI recomputation.
            for account_id in self._cash_by_account:
                self._db_write(
                    refresh_performance,
                    self._run_id,
                    account_id,
                    config=self._config,
                )
            self._performance_dirty = False

        # Periodic status notification (flags cached at init)
        if self._status_enabled:
            self._status_period_count += 1
            if self._status_period_count >= self._status_interval:
                self._status_period_count = 0
                for account_id, equity, drawdown, period_return in status_values:
                    account_positions = self._account_positions(account_id)
                    pos_str = "flat"
                    if account_positions:
                        parts = [
                            f"{position.side} {symbol}"
                            for symbol, position in account_positions.items()
                        ]
                        pos_str = ", ".join(parts)
                    self._notify(
                        "send_status",
                        strategy=(
                            f"{self._executor.strategy_name}:{account_id}"
                            f"({self._currency_by_account[account_id]})"
                        ),
                        symbol=",".join(account_positions),
                        equity=equity,
                        drawdown=drawdown,
                        daily_pnl=period_return * equity,
                        position=pos_str,
                    )

    def _flatten_account_and_halt(
        self,
        ts: datetime,
        account_id: str,
        drawdown: float,
        bars: dict[str, dict[str, float]],
        *,
        used_bar_quantity_by_symbol: dict[str, float] | None = None,
    ) -> None:
        """Queue or submit exits and halt new exposure for one account."""
        self._flatten_accounts_and_halt(
            ts,
            [(account_id, drawdown)],
            bars,
            used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
        )

    def _flatten_accounts_and_halt(
        self,
        ts: datetime,
        breaches: list[tuple[str, float]],
        bars: dict[str, dict[str, float]],
        *,
        used_bar_quantity_by_symbol: dict[str, float] | None = None,
    ) -> None:
        """Queue one serial recovery batch for all accounts breached together."""
        exit_queued = False
        breached_account_ids = {account_id for account_id, _ in breaches}
        account_positions = {
            symbol: position
            for symbol, position in self._positions.items()
            if self._account_id_by_symbol[symbol] in breached_account_ids
        }
        if self._executor.simulation:
            queue_market_exit_all(
                account_positions,
                reason=REASON_DRAWDOWN_BREACH,
            )
            flattened = not account_positions
            exit_queued = not flattened
        else:
            actions = [
                OrderIntent(action="close", symbol=symbol, reason=REASON_DRAWDOWN_BREACH)
                for symbol in account_positions
            ]
            reference_bars = {
                symbol: {"close": self._get_last_price(symbol, position)}
                for symbol, position in account_positions.items()
            }
            # Live emergency exits submit the full remaining quantity. Broker
            # execution reports remain authoritative for partial fills.
            flattened = self._execute_live_decision(
                actions,
                reference_bars,
                ts,
                apply_volume_limit=False,
            )
        self._halted_accounts.update(breached_account_ids)
        self._pending_decision = self._without_halted_accounts(
            self._pending_decision,
            primary_symbol=self._symbols[0],
        )
        self._persist_state()
        if flattened:
            outcome = "flattened account positions"
        elif exit_queued:
            outcome = "market exits queued for next observed opens"
        elif self._active_orders:
            outcome = "flattening in progress"
        else:
            outcome = "flatten attempt failed"
        for account_id, drawdown in breaches:
            self._notify(
                "send_alert",
                title=f"[{self._executor.strategy_name}] Max Drawdown Breach",
                message=(
                    f"account_id={account_id} drawdown={drawdown:.2%} <= "
                    f"-{self._risk_policy.max_drawdown_rate:.2%} — {outcome} and halted"
                ),
            )
            logger.warning(
                "LiveTrader account %s halted at %s: drawdown %.2f%% breached "
                "max_drawdown_rate=%.2f%% — %s",
                account_id,
                ts,
                drawdown * 100,
                self._risk_policy.max_drawdown_rate * 100,
                outcome,
            )
