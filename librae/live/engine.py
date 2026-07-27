"""LiveTrader — polling loop for sim and live modes.

Aligns completed bars into portfolio cycles, runs strategy once per timestamp,
and routes intents to LiveExecutor. Caches OHLCV to avoid redundant fetches.

Wiring is internalized: pass cfg=RunConfig to __init__,
the engine builds adapter, cost_model, callbacks, telegram internally.
Use on_bar=None etc. to disable specific callbacks (e.g. in tests).
"""

from __future__ import annotations

import logging
import signal
import time
import types
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar, Literal

import pandas as pd

from librae.core import EPSILON
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    ActionResults,
    apply_execution_fill,
    eval_equity,
    liquidate_all,
    process_actions,
    process_rebalance_targets,
    run_pending_and_stops,
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

from .executor import LiveExecutor, OrderRequest

if TYPE_CHECKING:
    from librae.core.cost_model import CostModel
    from librae.core.executor import OrderEvent
    from librae.core.run_config import RunConfig

logger = logging.getLogger(__name__)

OHLCVFetcher = Callable[..., pd.DataFrame]

_UNSET = object()  # sentinel: distinguish "not passed" from "explicitly passed None"


class LiveTrader:
    """Polling-based runner for sim/live modes.

    Args:
        strategy: Strategy instance (same as backtest).
        feature_fn: Callable(h1_base: DataFrame) -> DataFrame with entry_signal/exit_signal.
        cfg: RunConfig — the sole configuration source.
        adapter: OHLCVFetcher override. None -> build from cfg.market.
        order_adapter: Override for order placement. None -> auto-built from
            cfg.market + env credentials when cfg.mode == "live" (SHIOAJI_*
            for tw_futures, CRYPTO_* otherwise — same adapter instance used
            for fetching, reused for orders). Ignored in sim mode.
        cost_model: CostModel override. None -> build from cfg.market.
        on_bar: _UNSET -> build DB callback from cfg; None -> no callback; callable -> use it.
        on_order_event: Same pattern as on_bar.
        on_ohlcv: Same pattern as on_bar.
        on_heartbeat: Same pattern as on_bar.
        on_signal_outcome: Same pattern as on_bar.
        warmup_fetcher: _UNSET or None -> plain API fetch via adapter; callable ->
            use it (e.g. a DB-first fetcher supplied by the caller's data layer).
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
        adapter: OHLCVFetcher | None = None,
        order_adapter: object | None = None,
        cost_model: CostModel | None = None,
        notifier: object | None = _UNSET,
        on_bar: Callable[..., None] | object | None = _UNSET,
        on_order_event: Callable[..., None] | object | None = _UNSET,
        on_ohlcv: Callable[..., None] | object | None = _UNSET,
        on_heartbeat: Callable[..., None] | object | None = _UNSET,
        on_signal_outcome: Callable[..., None] | object | None = _UNSET,
        warmup_fetcher: Callable[..., pd.DataFrame] | object | None = _UNSET,
    ) -> None:
        from librae.core.cost_model import CostModel
        from librae.core.utils import generate_run_id, interval_to_timedelta, to_ccxt

        self._strategy = strategy
        self._feature_fn = feature_fn
        self._cfg = cfg
        self._symbols = cfg.symbols
        self._timeframe = to_ccxt(cfg.timeframe)
        self._interval_delta = interval_to_timedelta(self._timeframe)
        self._poll_seconds = cfg.poll_seconds

        # --- Build adapter ---
        if adapter is not None:
            self._fetcher = adapter
        elif cfg.market == "tw_futures":
            from brokers.shioaji_adapter import ShioajiAdapter

            # simulation is resolved from ShioajiCredentials.sandbox (SHIOAJI_SANDBOX
            # env var) — deliberately orthogonal to cfg.mode, same as CryptoAdapter's
            # sandbox: mode decides whether fills are mirrored as real orders at all,
            # sandbox decides which venue this session talks to. This lets "live" mode
            # (order submission enabled) be drilled end-to-end against Shioaji's paper
            # environment by setting SHIOAJI_SANDBOX=true, without touching real money.
            _shioaji = ShioajiAdapter()
            self._fetcher = lambda symbol, tf, limit, *, drop_incomplete=False: (
                _shioaji.fetch_ohlcv(symbol, tf, limit=limit)
            )
            # Shioaji also places orders — reuse the same authenticated
            # session for live mode unless the caller passed one explicitly.
            if order_adapter is None and cfg.mode == "live":
                order_adapter = _shioaji
        else:
            from brokers.crypto_adapter import CryptoAdapter, CryptoCredentials

            if cfg.mode == "live":
                # BINANCE_API_KEY/BINANCE_API_SECRET required for live — read-only
                # CryptoAdapter() (no creds) would fail place_order at trade time.
                # Prefix is Binance-specific by convention (see crypto_adapter.py
                # module docstring) — a second exchange would need its own prefix.
                _adapter = CryptoAdapter(credentials=CryptoCredentials.from_env("BINANCE"))
                # Reuse the same authenticated adapter for order placement — same
                # pattern as Shioaji above — unless the caller passed one explicitly.
                if order_adapter is None:
                    order_adapter = _adapter
            else:
                _adapter = CryptoAdapter()
            self._fetcher = lambda symbol, tf, limit, *, drop_incomplete=False: (
                _adapter.fetch_ohlcv(symbol, tf, limit, drop_incomplete=drop_incomplete)
            )

        # --- Build cost_model ---
        resolved_cm = CostModel.from_config(cfg, override=cost_model)

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
            resolved_cm,
            simulation=not is_live,
            telegram=resolved_notifier,
            strategy_name=strategy_name,
            order_adapter=order_adapter if is_live else None,
        )

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

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._consecutive_errors: int = 0
        self._db_write_failures: int = 0
        self._last_cycle_ts: datetime | None = None
        self._stale_alerted: dict[str, bool] = {}
        self._last_prices: dict[str, float] = {}
        self._positions: dict[str, PositionState] = {}
        self._cash: float = cfg.initial_balance
        self._fill_price: str = (cfg.params or {}).get("fill_price", "open")
        self._max_position_pct, self._max_drawdown_pct, self._max_volume_participation_pct = (
            validate_risk_params(cfg.params)
        )
        self._halted: bool = False
        self._pending_intent: StrategyIntent = []
        self._equity_peak: float = cfg.initial_balance
        self._prev_equity: float = cfg.initial_balance
        self._trade_count: int = 0
        self._period_index: int = 0
        self._status_period_count: int = 0
        self._running: bool = False

        # Cache status config
        tg_obj = self._executor.telegram
        self._status_enabled: bool = bool(tg_obj and tg_obj.notifications.status.enabled)
        self._status_interval: int = tg_obj.notifications.status.interval_periods if tg_obj else 0

        self._notify_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg")
        self._sleep = time.sleep  # instance attribute so tests can skip real delays

    # --- Callback builders ---

    def _db_write(self, fn: Callable[..., object], *args: object, **kwargs: object) -> None:
        """Best-effort DB write — swallows failures so a DB blip never
        interrupts the live trading loop (self._cash/self._positions are
        authoritative and independent of these writes). But a *sustained*
        outage must not stay invisible forever: CONSECUTIVE_ERROR_THRESHOLD
        in a row (same threshold as the poll-cycle error alert) fires one
        Telegram alert, then resets on the next success so it can fire
        again if the outage continues."""
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
        _event_seq = 0

        def on_order_event(event: OrderEvent) -> None:
            nonlocal _event_seq
            _event_seq += 1
            fields = asdict(event)
            fields["event_id"] = make_event_id(run_id, _event_seq)
            fields["run_id"] = run_id
            fields["strategy"] = strategy
            fields["mode"] = cfg.mode
            fields["timeframe"] = timeframe
            self._db_write(write_trade_event, **fields)
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
                        "open": bar.get("open", 0),
                        "high": bar.get("high", 0),
                        "low": bar.get("low", 0),
                        "close": bar.get("close", 0),
                        "volume": bar.get("volume", 0),
                    }
                ]
            ).set_index("ts")
            self._db_write(write_ohlcv, row, symbol, timeframe_, data_source=self._cfg.data_source)

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
        """Default warmup: plain API fetch via self._fetcher — librae has no
        data-access layer of its own to warm up from. A DB-first fetcher
        (read cached history, gap-fill from API) needs a data-access layer
        that lives outside librae; pass warmup_fetcher= explicitly for that."""

        def warmup_fetcher(symbol: str, tf_ccxt: str, limit: int) -> pd.DataFrame:
            return self._fetcher(symbol, tf_ccxt, limit, drop_incomplete=True)

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
        adapter = self._executor.order_adapter
        if adapter is None:
            return
        if self._adopt_broker_positions():
            return

        self._halted = True
        self._pending_intent = []
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Position Reconciliation Failed",
            message="Broker positions are unavailable; trading is halted.",
        )

    def _read_broker_positions(self) -> dict[str, PositionState]:
        """Read a complete broker position snapshot or raise."""
        adapter = self._executor.order_adapter
        if adapter is None:
            return {}

        positions: dict[str, PositionState] = {}
        for symbol in self._symbols:
            broker_pos = adapter.get_position(symbol)
            size = float(broker_pos.get("size") or 0)
            if not size:
                continue
            avg_price = float(broker_pos.get("avg_price") or 0.0)
            if avg_price <= 0:
                raise ValueError(f"broker returned invalid average price for {symbol}")
            side: Literal["long", "short"] = "long" if size > 0 else "short"
            quantity = abs(size)
            positions[symbol] = PositionState(
                symbol=symbol,
                side=side,
                entry_price=avg_price,
                quantity=quantity,
                entry_at=datetime.now(tz=UTC),
                periods_held=0,
                entry_commission=0.0,
                entry_slippage=0.0,
                entry_tax=0.0,
                total_entry_cost=avg_price * quantity * self._executor.cost_model.multiplier,
            )
        return positions

    def _adopt_broker_positions(self) -> bool:
        """Replace the local book atomically from a complete broker snapshot."""
        try:
            positions = self._read_broker_positions()
        except Exception:
            logger.exception("Broker position snapshot failed; keeping last confirmed local book")
            return False

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

    # Settlement currency per market, for symbols that aren't a CCXT
    # "BASE/QUOTE" pair (tw_futures/us_equity don't encode currency in the
    # symbol itself the way crypto pairs do).
    _MARKET_CURRENCY: ClassVar[dict[str, str]] = {"tw_futures": "TWD", "us_equity": "USD"}

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
        adapter = self._executor.order_adapter
        get_balance = getattr(adapter, "get_balance", None) if adapter else None
        if get_balance is None:
            return

        if "/" in self._symbols[0]:
            currency = self._symbols[0].split("/")[-1]
        else:
            currency = self._MARKET_CURRENCY.get(self._cfg.market)
            if currency is None:
                return

        try:
            balance = get_balance(currency)
        except Exception:
            logger.exception("Cash reconciliation failed for %s — skipping", currency)
            return

        broker_total = balance["total"]
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
        """Fail closed without inferring fills from a position snapshot."""
        self._halted = True
        self._pending_intent = []
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

    def _setup_signal_handlers(self) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""

        def _handler(signum: int, frame: types.FrameType | None) -> None:
            logger.info("Received signal %d, shutting down gracefully", signum)
            self.stop()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def _poll_cycle(self) -> None:
        """Fetch all symbols and process the latest aligned portfolio cycle."""
        if self._on_heartbeat:
            self._on_heartbeat(self._run_id)

        frames: dict[str, pd.DataFrame] = {}
        for symbol in self._symbols:
            df = self._fetch_with_cache(symbol)
            if df is None or df.empty:
                continue

            latest_ts = df["ts"].iloc[-1].to_pydatetime()
            self._check_staleness(symbol, latest_ts)
            frames[symbol] = df

        cycle_ts = self._latest_aligned_timestamp(frames)
        if cycle_ts is None:
            return

        # Mark the cycle before strategy execution. A callback/strategy failure
        # must not replay the same portfolio decision on the next poll.
        self._last_cycle_ts = cycle_ts
        logger.info("New portfolio cycle: %s", cycle_ts)

        for position in self._positions.values():
            position.periods_held += 1

        self._process_cycle(frames, cycle_ts)

    def _latest_aligned_timestamp(
        self,
        frames: dict[str, pd.DataFrame],
    ) -> datetime | None:
        """Return the common latest timestamp once every symbol reaches it."""
        if any(symbol not in frames for symbol in self._symbols):
            return None

        latest = [pd.Timestamp(frames[symbol]["ts"].iloc[-1]) for symbol in self._symbols]
        if len(set(latest)) != 1:
            return None

        cycle_ts = latest[0].to_pydatetime()
        if self._last_cycle_ts is not None and cycle_ts <= self._last_cycle_ts:
            return None
        return cycle_ts

    def _fetch_with_cache(self, symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV and keep a sorted, deduplicated rolling cache."""
        try:
            if symbol not in self._ohlcv_cache:
                if self._warmup_fetcher:
                    new_df = self._warmup_fetcher(symbol, self._timeframe, self._warmup_periods)
                else:
                    new_df = self._fetcher(
                        symbol,
                        self._timeframe,
                        self._warmup_periods,
                        drop_incomplete=True,
                    )
                cached = None
            else:
                cached = self._ohlcv_cache[symbol]
                new_df = self._fetcher(symbol, self._timeframe, 2, drop_incomplete=True)

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

    def _apply_action_results(self, result: ActionResults) -> None:
        """Publish side effects after portfolio state has been committed."""
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
            self._trade_count += 1
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
        self._apply_action_results(result)

    def _plan_live_orders(
        self,
        intent: StrategyIntent,
        bars: dict[str, dict[str, float]],
        ts: datetime,
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
            self._max_position_pct * self._prev_equity if self._max_position_pct else None
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
                max_volume_participation_pct=self._max_volume_participation_pct,
                get_volume=get_volume,
            )
            return [
                self._executor.request_from_event(event, sequence=index)
                for index, event in enumerate(result.events)
            ]

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
                max_volume_participation_pct=self._max_volume_participation_pct,
                get_volume=get_volume,
            )
            planning_cash += result.cash_delta
            sequence_offset = len(requests)
            requests.extend(
                self._executor.request_from_event(
                    event,
                    order_type=order_type,
                    limit_price=limit_price,
                    sequence=sequence_offset + index,
                )
                for index, event in enumerate(result.events)
            )
        return requests

    def _execute_live_intent(
        self,
        intent: StrategyIntent,
        bars: dict[str, dict[str, float]],
        ts: datetime,
    ) -> bool:
        """Submit current-cycle intent and commit only confirmed executions."""
        try:
            requests = self._plan_live_orders(intent, bars, ts)
        except ValueError as exc:
            self._halt_live(title="Unsupported Live Intent", message=str(exc))
            return False

        for request in requests:
            report = self._executor.submit_order(request)
            if report is None:
                self._halt_live(
                    title="Order Report Failure",
                    message=(
                        f"{request.side} {request.symbol} qty={request.quantity:.4f} "
                        "did not return a valid broker execution report"
                    ),
                )
                return False

            if report.has_fill:
                if report.average_price is None or report.executed_at is None:
                    self._halt_live(
                        title="Execution Report Incomplete",
                        message=f"{request.symbol} fill is missing price or execution time",
                    )
                    return False
                fill = Fill(
                    symbol=report.symbol,
                    side="long" if report.side == "buy" else "short",
                    price=report.average_price,
                    quantity=report.filled_quantity,
                    commission=report.commission,
                    slippage=report.slippage,
                    tax=report.tax,
                )
                try:
                    self._cash, result = apply_execution_fill(
                        self._positions,
                        self._cash,
                        fill,
                        report.executed_at,
                        order_side=report.side,
                        cost_model=self._get_cost_model(report.symbol),
                        reason=request.reason,
                    )
                except ValueError as exc:
                    self._halt_live(title="Execution Fill Conflict", message=str(exc))
                    return False
                self._apply_action_results(result)

            if report.status != "filled":
                self._halt_live(
                    title=f"Order {report.status.replace('_', ' ').title()}",
                    message=(
                        f"{request.symbol} order_id={report.order_id or 'unassigned'} "
                        f"filled={report.filled_quantity:.4f}/{report.requested_quantity:.4f}; "
                        "open-order continuation is not yet supported"
                    ),
                )
                return False
        return True

    def _process_bar(self, symbol: str, raw_df: pd.DataFrame, ts: datetime) -> None:
        """Process one symbol through the portfolio-cycle path."""
        self._process_cycle({symbol: raw_df}, ts)

    def _process_cycle(
        self,
        raw_frames: dict[str, pd.DataFrame],
        ts: datetime,
    ) -> None:
        """Execute and evaluate one synchronized portfolio timestamp.

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
            if history.empty:
                continue
            histories[symbol] = history
            raw_bar = history.iloc[-1].to_dict()
            raw_bars[symbol] = raw_bar
            self._last_prices[symbol] = float(raw_bar.get("close", 0.0))

        if self._executor.simulation:
            pending_intent = self._pending_intent
            self._pending_intent = []
            max_position_notional = (
                self._max_position_pct * self._prev_equity if self._max_position_pct else None
            )
            staged_cash, step_result = run_pending_and_stops(
                ts,
                self._positions,
                self._cash,
                pending_intent,
                raw_bars,
                get_cost_model=self._get_cost_model,
                default_fill=self._fill_price,
                primary_symbol=primary_symbol,
                max_position_notional=max_position_notional,
                max_volume_participation_pct=self._max_volume_participation_pct,
            )
            self._commit_simulated_results(
                cash=staged_cash,
                positions=self._positions,
                result=step_result,
            )

        # ── Step 1.5: equity/drawdown check — right after this bar's fills
        # and stops are applied, before the strategy sees the bar. Mirrors
        # the backtest engine's ordering so a drawdown breach halts new
        # entries on the same cycle it's detected, not one cycle later ──
        self._record_equity(ts)
        if self._halted:
            return

        bars: dict[str, dict[str, float]] = {}
        for symbol in self._symbols:
            try:
                featured = self._feature_fn(histories[symbol])
            except Exception:
                logger.exception(
                    "Feature computation failed for %s; skipping strategy decision for cycle %s",
                    symbol,
                    ts,
                )
                return

            bar = featured.iloc[-1].to_dict()
            bars[symbol] = bar
            price = float(bar.get("close", self._last_prices[symbol]))
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

        # Strategy sees only completed data. Simulation defers its intent to
        # the next bar; live submits now, before any future bar exists.
        equity, position_snapshot = self._eval_equity_snapshot()
        ctx = Context(
            ts=ts,
            symbol=primary_symbol,
            symbols=self._symbols,
            bar=bars[primary_symbol],
            bars=bars,
            positions=position_snapshot,
            cash=self._cash,
            equity=equity,
            period_index=self._period_index,
        )

        intent = self._strategy.on_bar(ctx)
        if self._executor.simulation:
            self._pending_intent = intent
        else:
            self._execute_live_intent(intent, raw_bars, ts)
        self._period_index += 1

        # Record OHLCV after processing (equity already recorded in Step 1.5)
        if self._on_ohlcv:
            for symbol, bar in bars.items():
                self._on_ohlcv(symbol, self._timeframe, bar, ts)

    def _eval_equity(self) -> float:
        """Total equity = cash + market value of all positions."""
        mtm, _ = self._eval_equity_snapshot()
        return mtm

    def _get_last_price(self, sym: str, ps: PositionState) -> float:
        return self._last_prices.get(sym, ps.entry_price)

    def _get_cost_model(self, _sym: str) -> CostModel:
        return self._executor.cost_model

    def _eval_equity_snapshot(self) -> tuple[float, dict[str, Position]]:
        """Shared MTM + snapshot computation."""
        return eval_equity(
            self._cash,
            self._positions,
            get_price=self._get_last_price,
            get_cost_model=self._get_cost_model,
        )

    def _record_equity(self, ts: datetime) -> None:
        """Calculate equity + drawdown, check the max-drawdown circuit
        breaker, call on_bar callback, and send periodic status."""
        equity = self._eval_equity()
        self._equity_peak = max(self._equity_peak, equity)
        drawdown = (
            (equity - self._equity_peak) / self._equity_peak if self._equity_peak > 0 else 0.0
        )
        period_return = (equity / self._prev_equity - 1.0) if self._prev_equity > 0 else 0.0
        self._prev_equity = equity

        if self._max_drawdown_pct and not self._halted and drawdown <= -self._max_drawdown_pct:
            self._flatten_and_halt(ts, drawdown)

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

    def _flatten_and_halt(self, ts: datetime, drawdown: float) -> None:
        """Force-close every open position and permanently halt new entries."""
        if self._executor.simulation:
            result = liquidate_all(
                self._positions,
                {},
                ts,
                get_cost_model=self._get_cost_model,
                reason=REASON_DRAWDOWN_BREACH,
                fallback_price=self._get_last_price,
            )
            self._commit_simulated_results(
                cash=self._cash + result.cash_delta,
                positions=self._positions,
                result=result,
            )
            flattened = True
        else:
            actions = [
                Action(type="close", symbol=symbol, reason=REASON_DRAWDOWN_BREACH)
                for symbol in self._positions
            ]
            bars = {
                symbol: {"close": self._get_last_price(symbol, position)}
                for symbol, position in self._positions.items()
            }
            flattened = self._execute_live_intent(actions, bars, ts)
        self._halted = True
        outcome = "flattened all positions" if flattened else "flatten attempt failed"
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Max Drawdown Breach",
            message=(
                f"drawdown={drawdown:.2%} <= -{self._max_drawdown_pct:.2%} — {outcome} and halted"
            ),
        )
        logger.warning(
            "LiveTrader halted at %s: drawdown %.2f%% breached max_drawdown_pct=%.2f%% — %s",
            ts,
            drawdown * 100,
            self._max_drawdown_pct * 100,
            outcome,
        )
