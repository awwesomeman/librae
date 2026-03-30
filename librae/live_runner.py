"""LiveRunner — polling loop for live signal monitoring.

Detects completed bars, runs strategy, and routes actions to LiveExecutor.
Supports multiple symbols. Caches OHLCV to avoid redundant fetches.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from .live_executor import LiveExecutor
from .strategy import Action, BaseStrategy, Context, Position

logger = logging.getLogger(__name__)

OHLCVFetcher = Callable[..., pd.DataFrame]


class LiveRunner:
    """Polling-based live signal monitor.

    Args:
        strategy: Strategy instance (same as backtest).
        symbols: List of symbols to monitor.
        fetcher: Callable(symbol, timeframe, limit, drop_incomplete=True) -> DataFrame.
        feature_fn: Callable(h1_base: DataFrame) -> DataFrame with entry_signal/exit_signal.
        executor: LiveExecutor for handling actions.
        timeframe: Candle interval (e.g. "1h").
        warmup_bars: Number of historical bars for indicator warm-up.
        initial_balance: Starting cash for position sizing.
        poll_interval: Seconds between poll cycles.
        on_signal: Optional callback(symbol, action, price, ts).
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        symbols: list[str],
        fetcher: OHLCVFetcher,
        feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
        executor: LiveExecutor,
        *,
        timeframe: str = "1h",
        warmup_bars: int = 720,
        initial_balance: float = 100_000.0,
        poll_interval: float = 60.0,
        on_signal: Callable[..., None] | None = None,
    ) -> None:
        self._strategy = strategy
        self._symbols = symbols
        self._fetcher = fetcher
        self._feature_fn = feature_fn
        self._executor = executor
        self._timeframe = timeframe
        self._warmup_bars = warmup_bars
        self._poll_interval = poll_interval
        self._on_signal = on_signal

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._last_bar_ts: dict[str, datetime] = {}
        self._positions: dict[str, Position] = {}
        self._bars_held: dict[str, int] = {}
        self._cash: float = initial_balance
        self._bar_indices: dict[str, int] = {}
        self._running: bool = False

    def run(self, max_iterations: int | None = None) -> None:
        """Start the polling loop. Blocks until stopped or max_iterations reached."""
        self._running = True
        self._setup_signal_handlers()
        iteration = 0

        logger.info(
            "LiveRunner started: symbols=%s, timeframe=%s, poll=%ss",
            self._symbols, self._timeframe, self._poll_interval,
        )

        while self._running:
            try:
                self._poll_cycle()
            except Exception:
                logger.exception("Error in poll cycle, will retry next interval")

            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                logger.info("Reached max_iterations=%d, stopping", max_iterations)
                break

            if self._running:
                time.sleep(self._poll_interval)

        logger.info("LiveRunner stopped")

    def stop(self) -> None:
        """Signal the runner to stop after the current cycle."""
        self._running = False

    def _setup_signal_handlers(self) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        def _handler(signum: int, frame: Any) -> None:
            logger.info("Received signal %d, shutting down gracefully", signum)
            self.stop()
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def _poll_cycle(self) -> None:
        """Single poll cycle: fetch data for each symbol, detect new bars, run strategy."""
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

            # Increment bars_held for existing positions
            if symbol in self._bars_held:
                self._bars_held[symbol] += 1

            self._process_bar(symbol, df, latest_ts)

    def _fetch_with_cache(self, symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV with caching. Full fetch on first call, incremental after."""
        try:
            if symbol not in self._ohlcv_cache:
                df = self._fetcher(
                    symbol, self._timeframe, self._warmup_bars, drop_incomplete=True,
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
                # Trim to keep only warmup_bars
                if len(cached) > self._warmup_bars:
                    cached = cached.iloc[-self._warmup_bars:]
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
        bar = {col: last_row[col] for col in featured.columns}
        price = float(bar.get("close", 0.0))

        # Rebuild Position with updated bars_held and unrealized_pnl
        self._update_positions(symbol, price)

        bar_index = self._bar_indices.get(symbol, 0)
        ctx = Context(
            ts=ts,
            instrument=symbol,
            instruments=self._symbols,
            bar=bar,
            bars={symbol: bar},
            positions=self._positions,
            cash=self._cash,
            bar_index=bar_index,
        )

        actions = self._strategy.on_bar(ctx)
        self._bar_indices[symbol] = bar_index + 1

        for action in actions:
            if action.type == "hold":
                continue

            if action.type == "close" and symbol in self._positions:
                pos = self._positions.pop(symbol)
                self._bars_held.pop(symbol, None)
                self._cash += price * pos.quantity
                self._executor.notify_exit(symbol, price)
                logger.info("Position closed: %s @ %.2f", symbol, price)
                if self._on_signal:
                    self._on_signal(symbol, action, price, ts)
                continue

            fill = self._executor.execute(action, price, self._cash)
            if fill and action.type in ("buy", "sell"):
                self._cash -= self._executor.cost_model.estimate_entry_outlay(
                    fill.price, fill.quantity,
                )
                self._positions[symbol] = Position(
                    instrument=symbol,
                    side=fill.side,
                    entry_price=fill.price,
                    quantity=fill.quantity,
                    entry_ts=ts,
                    bars_held=0,
                    unrealized_pnl=0.0,
                )
                self._bars_held[symbol] = 0
                logger.info(
                    "Position opened: %s %s @ %.2f qty=%.4f",
                    fill.side, symbol, fill.price, fill.quantity,
                )
                if self._on_signal:
                    self._on_signal(symbol, action, price, ts)

    def _update_positions(self, symbol: str, current_price: float) -> None:
        """Rebuild Position with current bars_held and unrealized_pnl."""
        if symbol not in self._positions:
            return
        old = self._positions[symbol]
        bars_held = self._bars_held.get(symbol, 0)
        direction = -1.0 if old.side == "short" else 1.0
        unrealized_pnl = (current_price - old.entry_price) * old.quantity * direction

        # WHY: Position is frozen, so we must recreate it with updated fields
        self._positions[symbol] = Position(
            instrument=old.instrument,
            side=old.side,
            entry_price=old.entry_price,
            quantity=old.quantity,
            entry_ts=old.entry_ts,
            bars_held=bars_held,
            unrealized_pnl=unrealized_pnl,
        )
