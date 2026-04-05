"""LiveTrader — polling loop for sim and live modes.

Detects completed bars, runs strategy, and routes actions to LiveExecutor.
Supports multiple symbols. Caches OHLCV to avoid redundant fetches.
"""
from __future__ import annotations

import logging
import signal
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from librae.core.executor import TradeResult, direction, process_actions
from librae.core.strategy import Action, BaseStrategy, Context, Position, PositionState

from .executor import LiveExecutor

logger = logging.getLogger(__name__)

OHLCVFetcher = Callable[..., pd.DataFrame]


class LiveTrader:
    """Polling-based runner for sim/live modes.

    Args:
        strategy: Strategy instance (same as backtest).
        symbols: List of symbols to track.
        fetcher: Callable(symbol, timeframe, limit, drop_incomplete=True) -> DataFrame.
        feature_fn: Callable(h1_base: DataFrame) -> DataFrame with entry_signal/exit_signal.
        executor: LiveExecutor for handling actions.
        run_id: Unique run identifier for DB writes.
        timeframe: Candle interval (e.g. "1h").
        warmup_periods: Number of historical bars for indicator warm-up.
        initial_balance: Starting cash for position sizing.
        poll_seconds: Seconds between poll cycles.
        on_bar: Optional callback(run_id, ts, equity, drawdown, ret_1d)
            called every completed bar for equity persistence.
        on_order_event: Optional callback(OrderEvent) called on every
            position lifecycle event (open/add/reduce/close).
        on_ohlcv: Optional callback(symbol, timeframe, bar_dict, ts)
            called every completed bar for OHLCV persistence.
        on_heartbeat: Optional callback(run_id) called every poll cycle
            to update liveness status.
        on_signal_outcome: Optional callback(symbol, ts, signal_value, price)
            called when signal_column has a non-NaN value.
        signal_column: Feature column name to capture as signal value.

    Lifecycle notifications (startup/shutdown/error) are sent via the
    executor's TelegramAdapter if configured.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        symbols: list[str],
        fetcher: OHLCVFetcher,
        feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
        executor: LiveExecutor,
        *,
        run_id: str = "",
        timeframe: str = "1h",
        warmup_periods: int = 720,
        initial_balance: float = 100_000.0,
        poll_seconds: float = 60.0,
        on_bar: Callable[..., None] | None = None,
        on_order_event: Callable[..., None] | None = None,
        on_ohlcv: Callable[..., None] | None = None,
        on_heartbeat: Callable[..., None] | None = None,
        on_signal_outcome: Callable[..., None] | None = None,
        signal_column: str | None = None,
        warmup_fetcher: Callable[..., pd.DataFrame] | None = None,
    ) -> None:
        self._strategy = strategy
        self._symbols = symbols
        self._fetcher = fetcher
        self._feature_fn = feature_fn
        self._executor = executor
        self._run_id = run_id
        self._timeframe = timeframe
        self._warmup_periods = warmup_periods
        self._poll_seconds = poll_seconds
        self._warmup_fetcher = warmup_fetcher
        self._on_bar = on_bar
        self._on_order_event = on_order_event
        self._on_ohlcv = on_ohlcv
        self._on_heartbeat = on_heartbeat
        self._on_signal_outcome = on_signal_outcome
        self._signal_column = signal_column

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._consecutive_errors: int = 0
        self._last_bar_ts: dict[str, datetime] = {}
        self._last_prices: dict[str, float] = {}
        self._positions: dict[str, PositionState] = {}
        self._cash: float = initial_balance
        self._equity_peak: float = initial_balance
        self._prev_equity: float = initial_balance
        self._trade_count: int = 0
        self._bar_indices: dict[str, int] = {}
        self._symbols_str: str = ",".join(symbols)
        self._status_bar_count: int = 0
        self._running: bool = False

        # Cache status config to avoid property-chain lookups on every bar
        tg = executor.telegram
        self._status_enabled: bool = bool(tg and tg.notifications.status.enabled)
        self._status_interval: int = tg.notifications.status.interval_bars if tg else 0

        # WHY: Telegram API calls block (10s timeout + retries).  A bounded
        # pool avoids spawning a new OS thread per notification while keeping
        # the main poll loop unblocked.
        self._notify_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg")

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

    def run(self, max_iterations: int | None = None) -> None:
        """Start the polling loop. Blocks until stopped or max_iterations reached."""
        self._running = True
        self._setup_signal_handlers()
        iteration = 0
        strategy_name = self._executor.strategy_name
        symbols_str = self._symbols_str

        logger.info(
            "LiveTrader started: symbols=%s, timeframe=%s, poll=%ss",
            self._symbols, self._timeframe, self._poll_seconds,
        )
        self._notify(
            "send_startup",
            strategy=strategy_name,
            symbol=symbols_str,
            mode="sim" if self._executor.simulation else "live",
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
            # WHY: wait=True ensures the shutdown notification is actually sent
            # before the process exits; without it the pool's threads get killed.
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

            # Increment bars_held on PositionState directly
            if symbol in self._positions:
                self._positions[symbol].bars_held += 1

            self._process_bar(symbol, df, latest_ts)

    def _fetch_with_cache(self, symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV with caching. Full fetch on first call, incremental after."""
        try:
            if symbol not in self._ohlcv_cache:
                if self._warmup_fetcher:
                    # WHY: DB-first warmup avoids re-fetching 720 bars from
                    # exchange API on every sim restart.
                    df = self._warmup_fetcher(symbol, self._timeframe, self._warmup_periods)
                else:
                    df = self._fetcher(
                        symbol, self._timeframe, self._warmup_periods, drop_incomplete=True,
                    )
                self._ohlcv_cache[symbol] = df
                return df

            # Incremental: fetch only last 2 bars
            new_df = self._fetcher(symbol, self._timeframe, 2, drop_incomplete=True)
            if new_df.empty:
                return self._ohlcv_cache[symbol]

            cached = self._ohlcv_cache[symbol]
            last_cached_ts = cached["ts"].iloc[-1]
            new_bars = new_df[new_df["ts"] > last_cached_ts]

            if not new_bars.empty:
                cached = pd.concat([cached, new_bars], ignore_index=True)
                # Trim to keep only warmup_periods
                if len(cached) > self._warmup_periods:
                    cached = cached.iloc[-self._warmup_periods:]
                self._ohlcv_cache[symbol] = cached

            return cached
        except Exception:
            logger.exception("Failed to fetch %s", symbol)
            return self._ohlcv_cache.get(symbol)

    def _process_bar(self, symbol: str, raw_df: pd.DataFrame, ts: datetime) -> None:
        """Run feature pipeline + strategy on a completed bar."""
        h1 = raw_df.set_index("ts")
        h1.index.name = "ts"
        try:
            featured = self._feature_fn(h1)
        except Exception:
            logger.exception("Feature computation failed for %s", symbol)
            return

        last_row = featured.iloc[-1]
        bar = last_row.to_dict()
        price = float(bar.get("close", 0.0))
        self._last_prices[symbol] = price

        # Capture signal from feature layer (before strategy decision)
        if self._signal_column and self._on_signal_outcome:
            sig = bar.get(self._signal_column)
            if sig is not None and not pd.isna(sig):
                self._on_signal_outcome(symbol, ts, float(sig), price)

        bar_index = self._bar_indices.get(symbol, 0)
        ctx = Context(
            ts=ts,
            symbol=symbol,
            symbols=self._symbols,
            bar=bar,
            bars={symbol: bar},
            positions=self._build_position_snapshot(),
            cash=self._cash,
            bar_index=bar_index,
        )

        actions = self._strategy.on_bar(ctx)
        self._bar_indices[symbol] = bar_index + 1

        result = process_actions(
            actions, self._positions, self._cash, ts,
            get_price=lambda s: self._last_prices.get(s),
            get_cost_model=lambda s: self._executor.cost_model,
            primary_symbol=symbol,
        )
        self._cash += result.cash_delta

        for event in result.events:
            logger.info("Order event: %s %s %s %.4f @ %.2f",
                        event.event_type, event.side, event.symbol,
                        event.quantity, event.price)
            if self._on_order_event:
                self._on_order_event(event)

        for trade in result.trades:
            self._trade_count += 1
            self._executor.notify_exit(trade.symbol, trade.exit_price)
            logger.info("Position closed: %s @ %.2f", trade.symbol, trade.exit_price)

        # Record equity and OHLCV after processing all actions
        self._record_equity(ts)
        if self._on_ohlcv:
            self._on_ohlcv(symbol, self._timeframe, bar, ts)

    def _build_position_snapshot(self) -> dict[str, Position]:
        """Convert mutable PositionState to frozen Position for Context."""
        cost_model = self._executor.cost_model
        snapshot: dict[str, Position] = {}
        for sym, ps in self._positions.items():
            price = self._last_prices.get(sym, ps.entry_price)
            unrealized = cost_model.calc_pnl(ps.entry_price, price, ps.quantity) * direction(ps.side)
            snapshot[sym] = Position(
                symbol=ps.symbol,
                side=ps.side,
                entry_price=ps.entry_price,
                quantity=ps.quantity,
                entry_ts=ps.entry_ts,
                bars_held=ps.bars_held,
                unrealized_pnl=unrealized,
            )
        return snapshot

    def _eval_equity(self) -> float:
        """Total equity = cash + market value of all positions."""
        cost_model = self._executor.cost_model
        mtm = 0.0
        for sym, pos in self._positions.items():
            price = self._last_prices.get(sym, pos.entry_price)
            # WHY: same formula as backtest — PnL + entry notional (fixes direction bug)
            mtm += cost_model.calc_pnl(pos.entry_price, price, pos.quantity) * direction(pos.side)
            mtm += pos.entry_price * pos.quantity * cost_model.multiplier
        return self._cash + mtm

    def _record_equity(self, ts: datetime) -> None:
        """Calculate equity + drawdown, call on_bar callback, and send periodic status."""
        equity = self._eval_equity()
        self._equity_peak = max(self._equity_peak, equity)
        drawdown = (equity - self._equity_peak) / self._equity_peak if self._equity_peak > 0 else 0.0
        ret_1d = (equity / self._prev_equity - 1.0) if self._prev_equity > 0 else 0.0
        self._prev_equity = equity

        if self._on_bar:
            self._on_bar(self._run_id, ts, equity, drawdown, ret_1d)

        # Periodic status notification (flags cached at init)
        if self._status_enabled:
            self._status_bar_count += 1
            if self._status_bar_count >= self._status_interval:
                self._status_bar_count = 0
                pos_str = "flat"
                if self._positions:
                    parts = [f"{ps.side} {sym}" for sym, ps in self._positions.items()]
                    pos_str = ", ".join(parts)
                self._notify(
                    "send_status",
                    strategy=self._executor.strategy_name,
                    symbol=self._symbols_str,
                    equity=equity,
                    drawdown=drawdown,
                    daily_pnl=ret_1d * equity,
                    position=pos_str,
                )

