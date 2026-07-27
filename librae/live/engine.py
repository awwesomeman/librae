"""LiveTrader — polling loop for sim and live modes.

Processes newly completed bars as data-driven events and routes intents to
LiveExecutor. Caches OHLCV to avoid redundant fetches.

Wiring is internalized: pass cfg=RunConfig to __init__,
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
from typing import TYPE_CHECKING, Literal

import pandas as pd

from librae.core import EPSILON
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    ActionResults,
    apply_execution_fill,
    eval_equity,
    liquidate_all,
    merge_pending_intents,
    partition_pending_intent,
    process_actions,
    process_rebalance_targets,
    run_pending_and_stops,
    validate_intent_symbols,
    validate_risk_params,
)
from librae.core.strategy import (
    Action,
    BaseStrategy,
    Context,
    Fill,
    Position,
    PositionState,
    RebalanceTargets,
    StrategyIntent,
)

from .executor import ExecutionReport, LiveExecutor, OrderRequest
from .state import LiveRuntimeState, LiveStateStore, TrackedOrder

if TYPE_CHECKING:
    from librae.core.cost_model import CostModel
    from librae.core.executor import OrderEvent
    from librae.core.run_config import RunConfig

logger = logging.getLogger(__name__)

OHLCVFetcher = Callable[..., pd.DataFrame]

_UNSET = object()  # sentinel: distinguish "not passed" from "explicitly passed None"
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
        cfg: RunConfig — the sole configuration source.
        adapter: Market-data fetcher override. None builds the data adapter
            from each symbol's explicit data_source route.
        order_adapter: Order gateway override. In live mode, omitting it
            requires an explicit cfg.broker or per-symbol broker route;
            execution is never inferred from the symbol or market.
        cost_model: CostModel override. None resolves one model per symbol.
        on_bar: _UNSET -> build DB callback from cfg; None -> no callback; callable -> use it.
        on_order_event: Same pattern as on_bar.
        on_ohlcv: Same pattern as on_bar.
        on_heartbeat: Same pattern as on_bar.
        on_signal_outcome: Same pattern as on_bar.
        warmup_fetcher: _UNSET or None -> plain API fetch via adapter; callable ->
            use it (e.g. a DB-first fetcher supplied by the caller's data layer).
        state_store: _UNSET -> TimescaleDB when DB is enabled; explicit
            duck-typed store -> use it. Live mode requires a store so
            placement attempts and fills survive process restarts.
        notifier: _UNSET -> build default TelegramAdapter from cfg (skipped
            entirely when cfg.no_db); None -> no notifications; object -> use it
            (must implement TelegramAdapter's duck-typed interface).
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
        *,
        cfg: RunConfig,
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
    ) -> None:
        from librae.config.symbols import resolve_symbol
        from librae.core.cost_model import CostModel
        from librae.core.utils import generate_run_id, interval_to_timedelta, to_ccxt

        self._strategy = strategy
        self._feature_fn = feature_fn
        self._cfg = cfg
        self._symbols = cfg.symbols
        self._timeframe = to_ccxt(cfg.timeframe)
        self._interval_delta = interval_to_timedelta(self._timeframe)
        self._poll_seconds = cfg.poll_seconds

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
                symbol: CostModel.from_config(cfg, symbol=symbol) for symbol in self._symbols
            }
        self._instruments = {
            symbol: resolve_symbol(
                cfg,
                symbol,
                multiplier=resolved_cost_models[symbol].multiplier,
            )
            for symbol in self._symbols
        }
        currencies = {instrument.currency for instrument in self._instruments.values()}
        if len(currencies) != 1:
            raise ValueError(
                "LiveTrader requires one accounting currency; configure an FX/base-currency "
                f"model before mixing {sorted(currencies)}"
            )

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
                    route = (cfg.instrument_overrides or {}).get(symbol, {})
                    broker = route.get("broker") or cfg.broker
                    instance = _build_builtin_adapter(
                        instrument.data_adapter,
                        trading=(
                            cfg.mode == "live"
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

        if cfg.mode == "live":
            if isinstance(order_adapter, Mapping):
                order_adapters = dict(order_adapter)
            elif order_adapter is not None:
                order_adapters = {symbol: order_adapter for symbol in self._symbols}
            else:
                broker_instances: dict[str, object] = {}
                order_adapters = {}
                for symbol, instrument in self._instruments.items():
                    route = (cfg.instrument_overrides or {}).get(symbol, {})
                    broker = route.get("broker") or cfg.broker
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
        else:
            order_adapters = {}

        # --- Resolve notifier (Telegram by default; injectable) ---
        # cfg.no_db gates this the same way it gates the db callbacks below
        # (dry_run implies no_db — see RunConfig.__post_init__), so a fully
        # local run never imports the notifications package.
        if notifier is not _UNSET:
            resolved_notifier = notifier
        elif cfg.no_db:
            resolved_notifier = None
        else:
            resolved_notifier = self._build_notifier()

        # --- Build run_id ---
        strategy_name = cfg.strategy_name
        self._run_id = generate_run_id(
            f"{strategy_name}_{cfg.market}",
            cfg.symbol,
            cfg.timeframe,
        )

        # --- Build executor ---
        is_live = cfg.mode == "live"
        self._executor = LiveExecutor(
            resolved_cost_models,
            simulation=not is_live,
            telegram=resolved_notifier,
            strategy_name=strategy_name,
            order_adapter=order_adapters if is_live else None,
            instruments=self._instruments,
        )

        # --- Restore restart-critical state before callbacks capture run_id ---
        self._state_key = f"{cfg.mode}:{cfg.config_hash}"
        if state_store is not _UNSET:
            self._state_store = state_store
        elif cfg.no_db:
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
        self._cash: float = cfg.initial_balance
        self._halted: bool = False
        self._pending_intent: StrategyIntent = []
        self._active_orders: list[TrackedOrder] = []
        self._equity_peak: float = cfg.initial_balance
        self._prev_equity: float = cfg.initial_balance
        self._trade_count: int = 0
        self._event_sequence: int = 0
        self._period_index: int = 0
        self._status_period_count: int = 0
        self._restored_state = False
        if self._state_store is not None:
            restored = self._state_store.load(self._state_key)
            if restored is not None:
                self._restore_state(restored)

        # --- Resolve callbacks (sentinel pattern) ---
        self._warmup_periods = (cfg.params or {}).get("warmup_periods", 720)

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
            elif cfg.no_db:
                setattr(self, f"_{attr}", None)
            else:
                setattr(self, f"_{attr}", builder())

        if not cfg.no_db:
            self._register_run()

        self._fill_price: str = (cfg.params or {}).get("fill_price", "open")
        self._risk_limits = validate_risk_params(cfg.params)
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

    def _snapshot_state(self) -> LiveRuntimeState:
        return LiveRuntimeState(
            state_key=self._state_key,
            run_id=self._run_id,
            config_hash=self._cfg.config_hash,
            mode=self._cfg.mode,
            cash=self._cash,
            positions=deepcopy(self._positions),
            last_prices=dict(self._last_prices),
            last_cycle_ts=self._last_cycle_ts,
            last_bar_ts=dict(self._last_bar_ts),
            pending_intent=deepcopy(self._pending_intent),
            active_orders=deepcopy(self._active_orders),
            equity_peak=self._equity_peak,
            prev_equity=self._prev_equity,
            trade_count=self._trade_count,
            event_sequence=self._event_sequence,
            period_index=self._period_index,
            status_period_count=self._status_period_count,
            halted=self._halted,
        )

    def _restore_state(self, state: LiveRuntimeState) -> None:
        if state.state_key != self._state_key:
            raise ValueError("runtime state key does not match this configuration")
        if state.config_hash != self._cfg.config_hash or state.mode != self._cfg.mode:
            raise ValueError("runtime state configuration does not match this run")
        self._run_id = state.run_id
        self._cash = state.cash
        self._positions = state.positions
        self._last_prices = state.last_prices
        self._last_cycle_ts = state.last_cycle_ts
        self._last_bar_ts = state.last_bar_ts
        self._pending_intent = state.pending_intent
        self._active_orders = state.active_orders
        self._equity_peak = state.equity_peak
        self._prev_equity = state.prev_equity
        self._trade_count = state.trade_count
        self._event_sequence = state.event_sequence
        self._period_index = state.period_index
        self._status_period_count = state.status_period_count
        self._halted = state.halted
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
                strategy=self._cfg.strategy_name,
                symbol=self._cfg.symbol,
                timeframe=self._cfg.timeframe,
                mode=self._cfg.mode,
                started_at=datetime.now(tz=UTC),
                data_source=self._cfg.data_source,
                poll_seconds=self._cfg.poll_seconds,
                params=self._cfg.params,
                perf_params=self._cfg.perf_params,
                # config_hash intentionally omitted: idx_backtest_runs_config_hash is a
                # unique index meant for backtest dedup (check_existing_run) -- sim/live
                # runs legitimately restart with an identical config (e.g. after a
                # crash) and must never collide on it.
            )
        except Exception as e:
            logger.warning("DB write_run_metadata failed: %s", e)

    def _build_on_bar(self) -> Callable:
        from db.timescale_writer import write_equity_curve_point

        strategy = self._cfg.strategy_name

        def on_bar(
            run_id_: str, ts: datetime, equity: float, drawdown: float, period_return: float
        ) -> None:
            self._db_write(
                write_equity_curve_point,
                ts=ts,
                run_id=run_id_,
                equity=equity,
                drawdown=drawdown,
                period_return=period_return,
                strategy=strategy,
            )

        return on_bar

    def _build_on_order_event(self) -> Callable:
        from db.timescale_writer import refresh_performance, write_trade_event

        from librae.core.utils import make_event_id

        run_id = self._run_id
        strategy = self._cfg.strategy_name
        timeframe = self._cfg.timeframe
        cfg = self._cfg

        def on_order_event(event: OrderEvent) -> None:
            self._event_sequence += 1
            fields = asdict(event)
            fields["event_id"] = make_event_id(run_id, self._event_sequence)
            fields["run_id"] = run_id
            fields["strategy"] = strategy
            fields["mode"] = cfg.mode
            fields["timeframe"] = timeframe
            self._db_write(write_trade_event, **fields)
            if not self._executor.simulation:
                self._persist_state()
            if event.event_type in ("close", "reduce"):
                self._db_write(refresh_performance, run_id, cfg=cfg)

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
        strategy = self._cfg.strategy_name
        timeframe = self._cfg.timeframe

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
                mode=self._cfg.mode,
                timeframe=timeframe,
                signal_value=signal_value,
                price=price,
                signal_type=signal_type,
            )

        return on_signal_event_cb

    def _build_notifier(self) -> object | None:
        """Default notifier: TelegramAdapter built from cfg.telegram_config
        + TELEGRAM_* env vars. Lazy import so a fully local run (cfg.no_db,
        or an explicit notifier= override) never touches the notifications
        package."""
        from notifications.config import TelegramConfig
        from notifications.telegram import TelegramAdapter, TelegramCredentials

        tg_config = TelegramConfig.from_dict(self._cfg.telegram_config or {})
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
    # tolerated on top of that before alerting. Pure monitoring, no effect
    # on trading, so it's an engine constant like CONSECUTIVE_ERROR_THRESHOLD
    # rather than a cfg.params opt-in.
    STALE_DATA_TOLERANCE_BARS = 2

    def _notify(self, method: str, **kwargs: object) -> None:
        """Submit a Telegram notification to the background thread pool."""
        telegram = self._executor.telegram
        if not telegram:
            return
        fn = getattr(telegram, method)
        self._notify_pool.submit(fn, **kwargs)

    def _check_staleness(self, symbol: str, latest_ts: datetime) -> None:
        """Alert if the latest fetched bar hasn't advanced in wall-clock
        time — catches a feed that stops updating without ever raising an
        exception (CONSECUTIVE_ERROR_THRESHOLD only covers raised errors).
        Edge-triggered: alerts once when crossing into stale, not every
        poll cycle, and re-arms once fresh data resumes.
        """
        age = datetime.now(UTC) - latest_ts
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

    def _reconcile_positions(self) -> None:
        """Adopt real broker positions into local state at startup.

        Without this, a process restart while a real position is open left
        self._positions/self._cash assuming flat/full-balance — the local
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
        if self._adopt_broker_positions():
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

    def _adopt_broker_positions(self) -> bool:
        """Adopt configured-symbol exposure on a first run."""
        try:
            snapshot = self._read_broker_positions()
        except Exception:
            logger.exception("Broker position snapshot failed; keeping last confirmed local book")
            return False

        positions: dict[str, PositionState] = {}
        for symbol, broker_position in snapshot.items():
            if broker_position.average_price is None:
                logger.error(
                    "Cannot adopt %s inventory without broker average cost; "
                    "restore a prior checkpoint or flatten the account",
                    symbol,
                )
                return False
            positions[symbol] = PositionState(
                symbol=symbol,
                side=broker_position.side,
                entry_price=broker_position.average_price,
                quantity=broker_position.quantity,
                entry_at=datetime.now(tz=UTC),
                periods_held=0,
                entry_commission=0.0,
                entry_slippage=0.0,
                entry_tax=0.0,
                total_entry_cost=broker_position.average_price
                * broker_position.quantity
                * self._executor.get_cost_model(symbol).multiplier,
            )
        self._positions = positions
        for symbol, position in positions.items():
            logger.warning(
                "Adopted broker position: %s %s %.4f @ %.2f",
                symbol,
                position.side,
                position.quantity,
                position.entry_price,
            )
        return True

    # 1% — same style as CONSECUTIVE_ERROR_THRESHOLD: an engine constant,
    # not a cfg.params knob (nothing in this run should reasonably need a
    # different tolerance).
    CASH_RECONCILE_TOLERANCE_PCT = 0.01

    def _reconcile_cash(self) -> None:
        """Best-effort: alert on cash/broker drift at startup, never
        auto-adjusts self._cash.

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

        currencies = {instrument.currency for instrument in self._instruments.values()}
        if len(currencies) != 1:
            logger.warning(
                "Cash reconciliation skipped for multi-currency run: %s",
                sorted(currencies),
            )
            return
        currency = next(iter(currencies))
        adapters: dict[int, object] = {}
        for symbol in self._symbols:
            adapter = self._executor.get_order_adapter(symbol)
            if callable(getattr(adapter, "get_balance", None)):
                adapters[id(adapter)] = adapter
        if not adapters:
            return

        try:
            broker_total = sum(
                float(adapter.get_balance(currency)["total"]) for adapter in adapters.values()
            )
        except Exception:
            logger.exception("Cash reconciliation failed for %s — skipping", currency)
            return

        if not isfinite(broker_total):
            logger.warning("Cash reconciliation returned a non-finite total — skipping")
            return
        drift_pct = abs(broker_total - self._cash) / max(self._cash, EPSILON)
        if drift_pct <= self.CASH_RECONCILE_TOLERANCE_PCT:
            return

        logger.warning(
            "Cash drift: local=%.2f broker=%.2f (%s), not auto-adjusted",
            self._cash,
            broker_total,
            currency,
        )
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Cash Reconciliation Drift",
            message=(
                f"local_cash={self._cash:.2f} broker_balance={broker_total:.2f} "
                f"({currency}) — drift {drift_pct:.2%}, review manually"
            ),
        )

    def _halt_live(self, *, title: str, message: str) -> None:
        """Fail closed and cancel every tracked order that may still execute."""
        self._halted = True
        self._pending_intent = []
        if not self._executor.simulation:
            self._cancel_active_orders()
        self._persist_state()
        logger.error("%s: %s", title, message)
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] {title}",
            message=f"{message}; trading halted.",
        )

    def run(self, max_iterations: int | None = None) -> None:
        """Start the polling loop. Blocks until stopped or max_iterations reached."""
        self._running = True
        self._setup_signal_handlers()
        if not self._executor.simulation:
            try:
                if self._halted:
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
            mode=self._cfg.mode,
            run_id=self._run_id,
        )

        shutdown_reason = "normal"
        try:
            while self._running:
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

                iteration += 1
                if max_iterations is not None and iteration >= max_iterations:
                    logger.info("Reached max_iterations=%d, stopping", max_iterations)
                    break

                if self._running:
                    time.sleep(self._poll_seconds)
        except Exception:
            shutdown_reason = "unhandled exception"
            logger.exception("LiveTrader crashed")
        finally:
            self._notify(
                "send_shutdown",
                strategy=strategy_name,
                symbol=symbols_str,
                reason=shutdown_reason,
            )
            self._notify_pool.shutdown(wait=True)
            logger.info("LiveTrader stopped (reason: %s)", shutdown_reason)

    def stop(self) -> None:
        """Signal the runner to stop after the current cycle."""
        self._running = False

    def reset_halt(self) -> None:
        """Start a new risk epoch after explicit operator review."""
        if self._active_orders:
            raise RuntimeError("cannot reset halt while broker orders remain unresolved")
        equity = self._eval_equity()
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
        """Process the newest completed bars without waiting for the full universe."""
        if not self._executor.simulation and self._active_orders:
            self._advance_live_orders()
            if self._active_orders or self._halted:
                return
        if self._on_heartbeat:
            self._on_heartbeat(self._run_id)

        frames: dict[str, pd.DataFrame] = {}
        latest_by_symbol: dict[str, datetime] = {}
        for symbol in self._symbols:
            df = self._fetch_with_cache(symbol)
            if df is None or df.empty:
                continue

            latest = pd.Timestamp(df["ts"].iloc[-1])
            if latest.tzinfo is None:
                raise ValueError(f"{symbol} latest completed bar timestamp must be timezone-aware")
            latest_ts = latest.to_pydatetime().astimezone(UTC)
            self._check_staleness(symbol, latest_ts)
            frames[symbol] = df
            latest_by_symbol[symbol] = latest_ts

        new_bars = {
            symbol: timestamp
            for symbol, timestamp in latest_by_symbol.items()
            if timestamp > self._last_bar_ts.get(symbol, datetime.min.replace(tzinfo=UTC))
        }
        if not new_bars:
            return

        cycle_ts = max(new_bars.values())
        for symbol, timestamp in new_bars.items():
            self._last_bar_ts[symbol] = timestamp

        if self._last_cycle_ts is not None and cycle_ts < self._last_cycle_ts:
            self._persist_state()
            return

        event_frames = {
            symbol: frames[symbol]
            for symbol, timestamp in new_bars.items()
            if timestamp == cycle_ts
        }
        self._last_cycle_ts = (
            cycle_ts if self._last_cycle_ts is None else max(self._last_cycle_ts, cycle_ts)
        )
        logger.info(
            "New market-data event: ts=%s symbols=%s",
            cycle_ts,
            sorted(event_frames),
        )
        self._process_cycle(event_frames, cycle_ts)

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
            self._ohlcv_cache[symbol] = merged
            return merged
        except Exception:
            logger.exception("Failed to fetch %s", symbol)
            return self._ohlcv_cache.get(symbol)

    def _publish_action_results(self, result: ActionResults) -> None:
        """Publish notifications and analytics after state is committed."""
        for event in result.events:
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
        cash: float,
        positions: dict[str, PositionState],
        result: ActionResults,
    ) -> None:
        """Commit a deterministic simulated fill batch."""
        self._cash = cash
        self._positions = positions
        self._trade_count += len(result.trades)
        self._publish_action_results(result)

    def _plan_live_orders(
        self,
        intent: StrategyIntent,
        bars: dict[str, dict[str, float]],
        ts: datetime,
        *,
        apply_volume_limit: bool = True,
    ) -> list[OrderRequest]:
        """Size intent at the latest completed close without inventing fills."""
        primary_symbol = self._symbols[0]
        prices = {
            symbol: float(bar["close"])
            for symbol, bar in bars.items()
            if bar.get("close") is not None and float(bar["close"]) > 0
        }

        def get_price(symbol: str, _action: Action) -> float | None:
            return prices.get(symbol)

        def get_volume(symbol: str) -> float | None:
            volume = bars.get(symbol, {}).get("volume")
            return float(volume) if volume is not None else None

        max_position_notional = (
            self._risk_limits.max_position_pct * self._prev_equity
            if self._risk_limits.max_position_pct
            else None
        )
        volume_limit = (
            self._risk_limits.max_volume_participation_pct if apply_volume_limit else None
        )
        staged_positions = deepcopy(self._positions)

        if isinstance(intent, RebalanceTargets):
            if intent.fill_price is not None:
                raise ValueError(
                    "Live RebalanceTargets.fill_price is unsupported; "
                    "target rebalances submit market orders after the completed-bar decision"
                )
            result = process_rebalance_targets(
                intent,
                staged_positions,
                self._cash,
                ts,
                get_price=get_price,
                get_cost_model=self._get_cost_model,
                primary_symbol=primary_symbol,
                max_position_notional=max_position_notional,
                max_volume_participation_pct=volume_limit,
                max_gross_exposure_pct=self._risk_limits.max_gross_exposure_pct,
                max_net_exposure_pct=self._risk_limits.max_net_exposure_pct,
                get_volume=get_volume,
            )
            requests = []
            for index, event in enumerate(result.events):
                request = self._executor.request_from_event(event, sequence=index)
                requests.append(
                    self._executor.prepare_order(
                        request,
                        reference_price=prices[event.symbol],
                    )
                )
            return requests

        requests: list[OrderRequest] = []
        planning_cash = self._cash
        for action in intent:
            if action.type == "hold":
                continue
            if action.stop_price is not None or action.take_profit_price is not None:
                raise ValueError(
                    "Live stop-loss/take-profit requires broker-native protective orders; "
                    "completed-bar range checks are simulation-only"
                )
            if isinstance(action.fill_price, str):
                raise ValueError(
                    "Live Action.fill_price cannot name a historical bar field; "
                    "use None for market or a numeric price for a broker limit order"
                )

            order_type = "limit" if isinstance(action.fill_price, (int, float)) else "market"
            limit_price = float(action.fill_price) if order_type == "limit" else None
            planning_action = replace(action, fill_price="close")
            result = process_actions(
                [planning_action],
                staged_positions,
                planning_cash,
                ts,
                get_price=get_price,
                get_cost_model=self._get_cost_model,
                primary_symbol=primary_symbol,
                max_position_notional=max_position_notional,
                max_volume_participation_pct=volume_limit,
                get_volume=get_volume,
            )
            planning_cash += result.cash_delta
            sequence_offset = len(requests)
            for index, event in enumerate(result.events):
                request = self._executor.request_from_event(
                    event,
                    order_type=order_type,
                    limit_price=limit_price,
                    sequence=sequence_offset + index,
                )
                requests.append(
                    self._executor.prepare_order(
                        request,
                        reference_price=prices[event.symbol],
                    )
                )
        return requests

    def _execute_live_intent(
        self,
        intent: StrategyIntent,
        bars: dict[str, dict[str, float]],
        ts: datetime,
        *,
        apply_volume_limit: bool = True,
    ) -> bool:
        """Persist a deterministic order queue, then advance it serially."""
        try:
            requests = self._plan_live_orders(
                intent,
                bars,
                ts,
                apply_volume_limit=apply_volume_limit,
            )
        except ValueError as exc:
            self._halt_live(title="Unsupported Live Intent", message=str(exc))
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

    def _advance_live_orders(self, *, submit_planned: bool = True) -> None:
        """Poll or submit the head order; dependent orders stay serialized."""
        while self._active_orders and not self._halted:
            tracked = self._active_orders[0]
            request = tracked.request
            if not tracked.placement_attempted:
                if not submit_planned:
                    return
                # Persist placement-attempted before network I/O. A crash in
                # the following call is recovered by client-order lookup and
                # never blindly retried.
                tracked.placement_attempted = True
                self._persist_state(tracked)
                report = self._executor.submit_order(request)
                if report is None:
                    report = self._executor.find_order(request)
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
                report = self._executor.get_order(request, tracked.order_id)
            else:
                report = self._executor.find_order(request)
                if report is None:
                    self._halt_live(
                        title="Ambiguous Restored Order",
                        message=(
                            f"{request.symbol} client_order_id={request.client_order_id} "
                            "was placement-attempted but cannot be found"
                        ),
                    )
                    return

            self._apply_order_report(tracked, report)
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
            if report.status != "filled":
                return

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

        result: ActionResults | None = None
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
                report = (
                    self._executor.get_order(tracked.request, tracked.order_id)
                    if tracked.order_id
                    else self._executor.find_order(tracked.request)
                )
                if report is None:
                    logger.error(
                        "Cannot cancel unresolved order %s",
                        tracked.request.client_order_id,
                    )
                    continue
                if report.status not in ("filled", "cancelled", "rejected"):
                    report = self._executor.cancel_order(tracked.request, report.order_id)
                self._apply_order_report(tracked, report)
            except Exception:
                logger.exception(
                    "Failed to cancel tracked order %s",
                    tracked.request.client_order_id,
                )

    def _process_bar(self, symbol: str, raw_df: pd.DataFrame, ts: datetime) -> None:
        """Process one symbol through the portfolio-cycle path."""
        self._process_cycle({symbol: raw_df}, ts)

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

        ready_intent, waiting_intent = partition_pending_intent(
            self._pending_intent,
            raw_bars,
            self._positions,
            primary_symbol=primary_symbol,
        )
        self._pending_intent = waiting_intent
        cycle_used_volume: dict[str, float] = {}
        if self._executor.simulation:
            max_position_notional = (
                self._risk_limits.max_position_pct * self._prev_equity
                if self._risk_limits.max_position_pct
                else None
            )
            if self._halted:
                staged_cash = self._cash
                step_result = ActionResults(trades=[], events=[], cash_delta=0.0)
            else:
                staged_cash, step_result = run_pending_and_stops(
                    ts,
                    self._positions,
                    self._cash,
                    ready_intent,
                    raw_bars,
                    get_cost_model=self._get_cost_model,
                    default_fill=self._fill_price,
                    primary_symbol=primary_symbol,
                    max_position_notional=max_position_notional,
                    max_volume_participation_pct=(self._risk_limits.max_volume_participation_pct),
                    max_gross_exposure_pct=self._risk_limits.max_gross_exposure_pct,
                    max_net_exposure_pct=self._risk_limits.max_net_exposure_pct,
                )
            self._commit_simulated_results(
                cash=staged_cash,
                positions=self._positions,
                result=step_result,
            )
            for event in step_result.events:
                cycle_used_volume[event.symbol] = (
                    cycle_used_volume.get(event.symbol, 0.0) + event.fill_quantity
                )
            if self._halted and self._positions:
                liquidation = liquidate_all(
                    self._positions,
                    raw_bars,
                    ts,
                    get_cost_model=self._get_cost_model,
                    reason=REASON_DRAWDOWN_BREACH,
                    max_volume_participation_pct=(self._risk_limits.max_volume_participation_pct),
                )
                self._commit_simulated_results(
                    cash=self._cash + liquidation.cash_delta,
                    positions=self._positions,
                    result=liquidation,
                )
        elif ready_intent and not self._execute_live_intent(ready_intent, raw_bars, ts):
            self._persist_state()
            return

        # ── Step 1.5: equity/drawdown check — right after this bar's fills
        # and stops are applied, before the strategy sees the bar. Mirrors
        # the backtest engine's ordering so a drawdown breach halts new
        # entries on the same cycle it's detected, not one cycle later ──
        self._record_equity(ts, raw_bars, used_volume=cycle_used_volume)
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
                    "Feature computation failed for %s; skipping strategy decision for cycle %s",
                    symbol,
                    ts,
                )
                return

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

        # A waiting target-weight basket is atomic; per-symbol Actions do not
        # prevent decisions for other symbols.
        if not isinstance(self._pending_intent, RebalanceTargets):
            equity, position_snapshot = self._eval_equity_snapshot()
            ctx = Context(
                ts=ts,
                symbol=primary_symbol,
                symbols=self._symbols,
                bar=bars.get(primary_symbol, {}),
                bars=bars,
                positions=position_snapshot,
                cash=self._cash,
                equity=equity,
                period_index=self._period_index,
            )
            intent = self._strategy.on_bar(ctx)
            validate_intent_symbols(
                intent,
                set(self._symbols),
                primary_symbol=primary_symbol,
            )
            self._period_index += 1
            if self._executor.simulation:
                self._pending_intent = merge_pending_intents(
                    self._pending_intent,
                    intent,
                    primary_symbol=primary_symbol,
                )
            else:
                ready_intent, waiting_intent = partition_pending_intent(
                    intent,
                    raw_bars,
                    self._positions,
                    primary_symbol=primary_symbol,
                )
                self._pending_intent = merge_pending_intents(
                    self._pending_intent,
                    waiting_intent,
                    primary_symbol=primary_symbol,
                )
                if ready_intent:
                    self._execute_live_intent(ready_intent, raw_bars, ts)

        for symbol, position in self._positions.items():
            if symbol in raw_bars:
                position.periods_held += 1
        self._persist_state()

        # Record OHLCV after processing (equity already recorded in Step 1.5)
        if self._on_ohlcv:
            for symbol, bar in raw_bars.items():
                self._on_ohlcv(symbol, self._timeframe, bar, ts)

    def _eval_equity(self) -> float:
        """Total equity = cash + market value of all positions."""
        mtm, _ = self._eval_equity_snapshot()
        return mtm

    def _get_last_price(self, sym: str, ps: PositionState) -> float:
        try:
            return self._last_prices[sym]
        except KeyError as exc:
            raise ValueError(f"no current valuation mark for open position {sym}") from exc

    def _get_cost_model(self, sym: str) -> CostModel:
        return self._executor.get_cost_model(sym)

    def _eval_equity_snapshot(self) -> tuple[float, dict[str, Position]]:
        """Shared MTM + snapshot computation."""
        return eval_equity(
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
        used_volume: dict[str, float] | None = None,
    ) -> None:
        """Calculate equity + drawdown, check the max-drawdown circuit
        breaker, call on_bar callback, and send periodic status."""
        equity = self._eval_equity()
        self._equity_peak = max(self._equity_peak, equity)
        drawdown = (
            (equity - self._equity_peak) / self._equity_peak if self._equity_peak > 0 else 0.0
        )
        period_return = (equity / self._prev_equity - 1.0) if self._prev_equity > 0 else 0.0
        self._prev_equity = equity

        if (
            self._risk_limits.max_drawdown_pct
            and not self._halted
            and drawdown <= -self._risk_limits.max_drawdown_pct
        ):
            self._flatten_and_halt(ts, drawdown, bars, used_volume=used_volume)

        if self._on_bar:
            self._on_bar(self._run_id, ts, equity, drawdown, period_return)

        # Periodic status notification (flags cached at init)
        if self._status_enabled:
            self._status_period_count += 1
            if self._status_period_count >= self._status_interval:
                self._status_period_count = 0
                pos_str = "flat"
                if self._positions:
                    parts = [f"{ps.side} {sym}" for sym, ps in self._positions.items()]
                    pos_str = ", ".join(parts)
                self._notify(
                    "send_status",
                    strategy=self._executor.strategy_name,
                    symbol=",".join(self._symbols),
                    equity=equity,
                    drawdown=drawdown,
                    daily_pnl=period_return * equity,
                    position=pos_str,
                )

    def _flatten_and_halt(
        self,
        ts: datetime,
        drawdown: float,
        bars: dict[str, dict[str, float]],
        *,
        used_volume: dict[str, float] | None = None,
    ) -> None:
        """Force-close every open position and permanently halt new entries."""
        if self._executor.simulation:
            result = liquidate_all(
                self._positions,
                bars,
                ts,
                get_cost_model=self._get_cost_model,
                reason=REASON_DRAWDOWN_BREACH,
                max_volume_participation_pct=(self._risk_limits.max_volume_participation_pct),
                used_volume=used_volume,
            )
            self._commit_simulated_results(
                cash=self._cash + result.cash_delta,
                positions=self._positions,
                result=result,
            )
            flattened = not self._positions
        else:
            actions = [
                Action(type="close", symbol=symbol, reason=REASON_DRAWDOWN_BREACH)
                for symbol in self._positions
            ]
            reference_bars = {
                symbol: {"close": self._get_last_price(symbol, position)}
                for symbol, position in self._positions.items()
            }
            # Live emergency exits submit the full remaining quantity. Broker
            # execution reports remain authoritative for partial fills.
            flattened = self._execute_live_intent(
                actions,
                reference_bars,
                ts,
                apply_volume_limit=False,
            )
        self._halted = True
        if not self._executor.simulation:
            self._cancel_active_orders()
        self._persist_state()
        outcome = "flattened all positions" if flattened else "flatten attempt failed"
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Max Drawdown Breach",
            message=(
                f"drawdown={drawdown:.2%} <= "
                f"-{self._risk_limits.max_drawdown_pct:.2%} — {outcome} and halted"
            ),
        )
        logger.warning(
            "LiveTrader halted at %s: drawdown %.2f%% breached max_drawdown_pct=%.2f%% — %s",
            ts,
            drawdown * 100,
            self._risk_limits.max_drawdown_pct * 100,
            outcome,
        )
