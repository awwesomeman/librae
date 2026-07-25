"""LiveTrader — polling loop for sim and live modes.

Detects completed bars, runs strategy, and routes actions to LiveExecutor.
Supports multiple symbols. Caches OHLCV to avoid redundant fetches.

Wiring is internalized: pass cfg=RunConfig to __init__,
the engine builds adapter, cost_model, callbacks, telegram internally.
Use on_bar=None etc. to disable specific callbacks (e.g. in tests).
"""
from __future__ import annotations

import logging
import signal
import time
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal

import pandas as pd

from librae.core.executor import (
    REASON_DRAWDOWN_BREACH, ActionResults, eval_equity, liquidate_all,
    run_pending_and_stops, validate_risk_params,
)
from librae.core.strategy import Action, BaseStrategy, Context, Position, PositionState

from .executor import LiveExecutor

if TYPE_CHECKING:
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
        warmup_fetcher: _UNSET -> DB-first warmup; None -> API-only; callable -> use it.
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
        on_bar: Callable[..., None] | None | object = _UNSET,
        on_order_event: Callable[..., None] | None | object = _UNSET,
        on_ohlcv: Callable[..., None] | None | object = _UNSET,
        on_heartbeat: Callable[..., None] | None | object = _UNSET,
        on_signal_outcome: Callable[..., None] | None | object = _UNSET,
        warmup_fetcher: Callable[..., pd.DataFrame] | None | object = _UNSET,
    ) -> None:
        from librae.config.notification import TelegramConfig
        from librae.core.cost_model import CostModel
        from librae.core.utils import generate_run_id, to_ccxt
        from librae.notifications.telegram import TelegramAdapter, TelegramCredentials

        self._strategy = strategy
        self._feature_fn = feature_fn
        self._cfg = cfg
        self._symbols = cfg.symbols
        self._timeframe = to_ccxt(cfg.timeframe)
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

        # --- Build telegram ---
        tg_config = TelegramConfig.from_dict(cfg.telegram_config or {})
        tg_creds = TelegramCredentials.from_env("TELEGRAM")
        telegram = TelegramAdapter(config=tg_config, credentials=tg_creds)
        # Disable telegram if dry_run
        if cfg.dry_run:
            telegram = None

        # --- Build run_id ---
        strategy_name = cfg.strategy_name
        self._run_id = generate_run_id(
            f"{strategy_name}_{cfg.market}", cfg.symbol, cfg.timeframe,
        )

        # --- Build executor ---
        is_live = cfg.mode == "live"
        self._executor = LiveExecutor(
            resolved_cm, simulation=not is_live,
            telegram=telegram, strategy_name=strategy_name,
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
        self._last_bar_ts: dict[str, datetime] = {}
        self._last_prices: dict[str, float] = {}
        self._positions: dict[str, PositionState] = {}
        self._cash: float = cfg.initial_balance
        self._fill_price: str = (cfg.params or {}).get("fill_price", "open")
        self._max_position_pct, self._max_drawdown_pct = validate_risk_params(cfg.params)
        self._halted: bool = False
        self._pending_actions: dict[str, list[Action]] = {}
        self._equity_peak: float = cfg.initial_balance
        self._prev_equity: float = cfg.initial_balance
        self._trade_count: int = 0
        self._period_indices: dict[str, int] = {}
        self._status_period_count: int = 0
        self._running: bool = False

        # Cache status config
        tg_obj = self._executor.telegram
        self._status_enabled: bool = bool(tg_obj and tg_obj.notifications.status.enabled)
        self._status_interval: int = tg_obj.notifications.status.interval_periods if tg_obj else 0

        self._notify_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg")

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
            logger.warning("DB %s failed (%d consecutive): %s", fn.__name__, self._db_write_failures, e)
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
                started_at=datetime.now(tz=timezone.utc),
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

        def on_bar(run_id_: str, ts: datetime, equity: float, drawdown: float, period_return: float) -> None:
            self._db_write(
                write_equity_curve_point, ts=ts, run_id=run_id_, equity=equity,
                drawdown=drawdown, period_return=period_return, strategy=strategy,
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

        def on_order_event(event: "OrderEvent") -> None:
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
            row = pd.DataFrame([{
                "ts": ts,
                "open": bar.get("open", 0),
                "high": bar.get("high", 0),
                "low": bar.get("low", 0),
                "close": bar.get("close", 0),
                "volume": bar.get("volume", 0),
            }]).set_index("ts")
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
            symbol: str, ts: datetime, signal_value: float, price: float,
            signal_type: str = "entry",
        ) -> None:
            self._db_write(
                write_signal_event,
                ts=ts, run_id=run_id, strategy=strategy, symbol=symbol,
                mode=self._cfg.mode, timeframe=timeframe,
                signal_value=signal_value, price=price,
                signal_type=signal_type,
            )
        return on_signal_event_cb

    def _build_warmup_fetcher(self) -> Callable:
        def warmup_fetcher(symbol: str, tf_ccxt: str, limit: int) -> pd.DataFrame:
            """DB-first warmup: reads historical bars from DB, fills gaps from API."""
            from strategies.module.data.ohlcv import get_ohlcv
            from librae.core.utils import interval_to_timedelta

            warmup_start = datetime.now(tz=timezone.utc) - interval_to_timedelta(tf_ccxt) * limit
            df = get_ohlcv(symbol, tf_ccxt, data_source=self._cfg.data_source,
                           start=warmup_start.isoformat())
            if df.empty:
                return self._fetcher(symbol, tf_ccxt, limit, drop_incomplete=True)
            if "timestamp" in df.columns and "ts" not in df.columns:
                df = df.rename(columns={"timestamp": "ts"})
            if len(df) > limit:
                df = df.iloc[-limit:]
            return df.reset_index(drop=True)
        return warmup_fetcher

    # WHY: 3 consecutive errors likely means a persistent issue (API down, DB
    # unreachable), not a transient blip — worth alerting the operator.
    CONSECUTIVE_ERROR_THRESHOLD = 3

    def _notify(self, method: str, **kwargs: object) -> None:
        """Submit a Telegram notification to the background thread pool."""
        telegram = self._executor.telegram
        if not telegram:
            return
        fn = getattr(telegram, method)
        self._notify_pool.submit(fn, **kwargs)

    def _reconcile_positions(self) -> None:
        """Adopt real broker positions into local state at startup.

        Without this, a process restart while a real position is open left
        self._positions/self._cash assuming flat/full-balance — the local
        book and the broker's actual book could silently diverge forever
        (double-open on the broker, or reject a legitimate opposite-side
        signal while the broker is actually flat).

        No-op in sim mode (order_adapter is None there — nothing to
        reconcile against). Only reconciles *positions*, not cash: no
        adapter currently exposes an account-balance query, so self._cash
        still starts from cfg.initial_balance — a known, separate gap.
        """
        adapter = self._executor.order_adapter
        if adapter is None:
            return
        for symbol in self._symbols:
            try:
                broker_pos = adapter.get_position(symbol)
                size = float(broker_pos.get("size") or 0)
                if not size:
                    continue

                side: Literal["long", "short"] = "long" if size > 0 else "short"
                avg_price = float(broker_pos.get("avg_price") or 0.0)
                quantity = abs(size)
                self._positions[symbol] = PositionState(
                    symbol=symbol, side=side, entry_price=avg_price, quantity=quantity,
                    entry_at=datetime.now(tz=timezone.utc), periods_held=0,
                    entry_commission=0.0, entry_slippage=0.0, entry_tax=0.0,
                    total_entry_cost=avg_price * quantity * self._executor.cost_model.multiplier,
                )
                logger.warning(
                    "Reconciled %s position from broker at startup: %s %.4f @ %.2f "
                    "(cash NOT adjusted — no account-balance API available; "
                    "initial_balance is used as-is)",
                    symbol, side, quantity, avg_price,
                )
            except Exception:
                logger.exception(
                    "Position reconciliation failed for %s — starting flat "
                    "for this symbol, verify manually", symbol,
                )

    def run(self, max_iterations: int | None = None) -> None:
        """Start the polling loop. Blocks until stopped or max_iterations reached."""
        self._running = True
        self._setup_signal_handlers()
        self._reconcile_positions()
        iteration = 0
        strategy_name = self._executor.strategy_name
        symbols_str = ",".join(self._symbols)

        logger.info(
            "LiveTrader started: symbols=%s, timeframe=%s, poll=%ss",
            self._symbols, self._timeframe, self._poll_seconds,
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
        """Single poll cycle: fetch data for each symbol, detect new bars, run strategy."""
        if self._on_heartbeat:
            self._on_heartbeat(self._run_id)

        for symbol in self._symbols:
            df = self._fetch_with_cache(symbol)
            if df is None or df.empty:
                continue

            latest_ts = df["ts"].iloc[-1].to_pydatetime()
            prev_ts = self._last_bar_ts.get(symbol)

            if prev_ts is not None and latest_ts <= prev_ts:
                continue  # No new completed bar

            self._last_bar_ts[symbol] = latest_ts
            logger.info("New bar detected: %s @ %s", symbol, latest_ts)

            # Increment periods_held on PositionState directly
            if symbol in self._positions:
                self._positions[symbol].periods_held += 1

            self._process_bar(symbol, df, latest_ts)

    def _fetch_with_cache(self, symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV with caching. Full fetch on first call, incremental after."""
        try:
            if symbol not in self._ohlcv_cache:
                if self._warmup_fetcher:
                    df = self._warmup_fetcher(symbol, self._timeframe, self._warmup_periods)
                else:
                    df = self._fetcher(
                        symbol, self._timeframe, self._warmup_periods, drop_incomplete=True,
                    )
                self._ohlcv_cache[symbol] = df
                return df

            new_df = self._fetcher(symbol, self._timeframe, 2, drop_incomplete=True)
            if new_df.empty:
                return self._ohlcv_cache[symbol]

            cached = self._ohlcv_cache[symbol]
            last_cached_ts = cached["ts"].iloc[-1]
            new_bars = new_df[new_df["ts"] > last_cached_ts]

            if not new_bars.empty:
                cached = pd.concat([cached, new_bars], ignore_index=True)
                if len(cached) > self._warmup_periods:
                    cached = cached.iloc[-self._warmup_periods:]
                self._ohlcv_cache[symbol] = cached

            return cached
        except Exception:
            logger.exception("Failed to fetch %s", symbol)
            return self._ohlcv_cache.get(symbol)

    def _apply_action_results(self, result: ActionResults) -> None:
        """Apply event/trade side effects (DB recording, live order
        submission, Telegram alerts) from a combined run_pending_and_stops()
        result — cash is already applied by the caller via that function's
        returned value, not here, so a stop-loss/take-profit fill goes
        through the exact same recording/submission/alerting path as a
        strategy-issued close, not a separate hand-rolled copy."""
        for event in result.events:
            logger.info("Order event: %s %s %s %.4f @ %.2f",
                        event.event_type, event.side, event.symbol,
                        event.fill_quantity, event.price)
            if self._on_order_event:
                self._on_order_event(event)

            if event.event_type in ("open", "add"):
                self._executor.notify_entry(event.symbol, event.side, event.price, event.event_type)

            if not self._executor.simulation:
                order_result = self._executor.submit_order(event)
                if order_result is None:
                    self._notify(
                        "send_alert",
                        title=f"[{self._executor.strategy_name}] Order Failed",
                        message=(
                            f"{event.event_type} {event.symbol} "
                            f"qty={event.fill_quantity:.4f} — check broker manually"
                        ),
                    )

        for trade in result.trades:
            self._trade_count += 1
            self._executor.notify_exit(trade.symbol, trade.exit_price)
            logger.info("Position closed: %s @ %.2f", trade.symbol, trade.exit_price)

    def _process_bar(self, symbol: str, raw_df: pd.DataFrame, ts: datetime) -> None:
        """Run feature pipeline + strategy on a completed bar.

        Uses pending-then-execute pattern to eliminate look-ahead bias:
        previous bar's actions are filled at *this* bar's price, then the
        strategy sees this bar and produces the *next* bar's pending actions.

        Order execution (pending-action fills, stop-loss/take-profit) runs
        on raw OHLCV and does not depend on feature computation succeeding
        — a feature-pipeline failure must not leave a real pending fill
        silently deferred to a later, unrelated bar's price, and must not
        block stop-loss/take-profit enforcement.
        """
        h1 = raw_df.set_index("ts")
        h1.index.name = "ts"
        raw_bar = h1.iloc[-1].to_dict()
        price = float(raw_bar.get("close", 0.0))
        self._last_prices[symbol] = price

        # ── Steps 1+1.5: fill previous bar's pending actions at current
        # bar's price, then check stop-loss/take-profit — shared with the
        # backtest engine (librae.core.executor.run_pending_and_stops) so
        # the two can't silently drift out of sync on this sequence ──
        prev_actions = self._pending_actions.pop(symbol, [])
        max_position_notional = (
            self._max_position_pct * self._prev_equity if self._max_position_pct else None
        )
        self._cash, step_result = run_pending_and_stops(
            ts, self._positions, self._cash, prev_actions, {symbol: raw_bar},
            get_cost_model=self._get_cost_model,
            default_fill=self._fill_price,
            primary_symbol=symbol,
            max_position_notional=max_position_notional,
        )
        self._apply_action_results(step_result)

        # ── Step 1.5: equity/drawdown check — right after this bar's fills
        # and stops are applied, before the strategy sees the bar. Mirrors
        # the backtest engine's ordering so a drawdown breach halts new
        # entries on the same cycle it's detected, not one cycle later ──
        self._record_equity(ts)
        if self._halted:
            return

        try:
            featured = self._feature_fn(h1)
        except Exception:
            logger.exception(
                "Feature computation failed for %s — pending fills/stops above "
                "still applied, skipping strategy decision for this bar", symbol,
            )
            return

        last_row = featured.iloc[-1]
        bar = last_row.to_dict()
        price = float(bar.get("close", price))
        self._last_prices[symbol] = price

        if self._on_signal_outcome:
            sig = bar.get("entry_signal")
            if sig is not None and not pd.isna(sig) and float(sig) != 0:
                self._on_signal_outcome(symbol, ts, float(sig), price)
            exit_sig = bar.get("exit_signal")
            if exit_sig is not None and not pd.isna(exit_sig) and float(exit_sig) != 0:
                self._on_signal_outcome(symbol, ts, float(exit_sig), price, signal_type="exit")

        # ── Step 2: strategy decision (produces next bar's pending actions) ──
        period_index = self._period_indices.get(symbol, 0)
        ctx = Context(
            ts=ts,
            symbol=symbol,
            symbols=self._symbols,
            bar=bar,
            bars={symbol: bar},
            positions=self._build_position_snapshot(),
            cash=self._cash,
            period_index=period_index,
        )

        actions = self._strategy.on_bar(ctx)
        self._period_indices[symbol] = period_index + 1
        self._pending_actions[symbol] = actions

        # Record OHLCV after processing (equity already recorded in Step 1.5)
        if self._on_ohlcv:
            self._on_ohlcv(symbol, self._timeframe, bar, ts)

    def _build_position_snapshot(self) -> dict[str, Position]:
        """Convert mutable PositionState to frozen Position for Context."""
        _, snapshot = self._eval_equity_snapshot()
        return snapshot

    def _eval_equity(self) -> float:
        """Total equity = cash + market value of all positions."""
        mtm, _ = self._eval_equity_snapshot()
        return mtm

    def _get_last_price(self, sym: str, ps: PositionState) -> float:
        return self._last_prices.get(sym, ps.entry_price)

    def _get_cost_model(self, _sym: str) -> "CostModel":
        return self._executor.cost_model

    def _eval_equity_snapshot(self) -> tuple[float, dict[str, Position]]:
        """Shared MTM + snapshot computation."""
        return eval_equity(
            self._cash, self._positions,
            get_price=self._get_last_price,
            get_cost_model=self._get_cost_model,
        )

    def _record_equity(self, ts: datetime) -> None:
        """Calculate equity + drawdown, check the max-drawdown circuit
        breaker, call on_bar callback, and send periodic status."""
        equity = self._eval_equity()
        self._equity_peak = max(self._equity_peak, equity)
        drawdown = (equity - self._equity_peak) / self._equity_peak if self._equity_peak > 0 else 0.0
        period_return = (equity / self._prev_equity - 1.0) if self._prev_equity > 0 else 0.0
        self._prev_equity = equity

        if (
            self._max_drawdown_pct and not self._halted
            and drawdown <= -self._max_drawdown_pct
        ):
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
        """Force-close every open position and permanently halt new entries
        for the rest of this run — the max-drawdown circuit breaker. Reuses
        _apply_action_results so live mode submits a real closing order to
        the broker through the exact same path as any other close."""
        result = liquidate_all(
            self._positions, {}, ts,
            get_cost_model=self._get_cost_model,
            reason=REASON_DRAWDOWN_BREACH,
            fallback_price=self._get_last_price,
        )
        self._cash += result.cash_delta
        self._apply_action_results(result)
        self._halted = True
        self._notify(
            "send_alert",
            title=f"[{self._executor.strategy_name}] Max Drawdown Breach",
            message=(
                f"drawdown={drawdown:.2%} <= -{self._max_drawdown_pct:.2%} "
                "— flattened all positions and halted"
            ),
        )
        logger.warning(
            "LiveTrader halted at %s: drawdown %.2f%% breached max_drawdown_pct=%.2f%% "
            "— all positions force-closed",
            ts, drawdown * 100, self._max_drawdown_pct * 100,
        )
