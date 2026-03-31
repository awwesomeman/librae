"""LiveRunner — polling loop for sim and live modes.

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

from .executor import LiveExecutor
from librae.core.strategy import Action, BaseStrategy, Context, Position

logger = logging.getLogger(__name__)

OHLCVFetcher = Callable[..., pd.DataFrame]


class LiveRunner:
    """Polling-based runner for sim/live modes.

    Args:
        strategy: Strategy instance (same as backtest).
        symbols: List of symbols to track.
        fetcher: Callable(symbol, timeframe, limit, drop_incomplete=True) -> DataFrame.
        feature_fn: Callable(h1_base: DataFrame) -> DataFrame with entry_signal/exit_signal.
        executor: LiveExecutor for handling actions.
        run_id: Unique run identifier for DB writes.
        timeframe: Candle interval (e.g. "1h").
        warmup_bars: Number of historical bars for indicator warm-up.
        initial_balance: Starting cash for position sizing.
        poll_interval: Seconds between poll cycles.
        on_signal: Optional callback(symbol, action, price, ts).
        on_bar: Optional callback(run_id, ts, equity, drawdown, ret_1d)
            called every completed bar for equity persistence.
        on_trade: Optional callback(trade_dict) called on position close.
        on_ohlcv: Optional callback(run_id, symbol, timeframe, bar_dict, ts)
            called every completed bar for OHLCV persistence.
        on_heartbeat: Optional callback(run_id) called every poll cycle
            to update liveness status.
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
        warmup_bars: int = 720,
        initial_balance: float = 100_000.0,
        poll_interval: float = 60.0,
        on_signal: Callable[..., None] | None = None,
        on_bar: Callable[..., None] | None = None,
        on_trade: Callable[..., None] | None = None,
        on_ohlcv: Callable[..., None] | None = None,
        on_heartbeat: Callable[..., None] | None = None,
    ) -> None:
        self._strategy = strategy
        self._symbols = symbols
        self._fetcher = fetcher
        self._feature_fn = feature_fn
        self._executor = executor
        self._run_id = run_id
        self._timeframe = timeframe
        self._warmup_bars = warmup_bars
        self._poll_interval = poll_interval
        self._on_signal = on_signal
        self._on_bar = on_bar
        self._on_trade = on_trade
        self._on_ohlcv = on_ohlcv
        self._on_heartbeat = on_heartbeat

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._last_bar_ts: dict[str, datetime] = {}
        self._last_prices: dict[str, float] = {}
        self._positions: dict[str, Position] = {}
        self._bars_held: dict[str, int] = {}
        self._cash: float = initial_balance
        self._equity_peak: float = initial_balance
        self._prev_equity: float = initial_balance
        self._trade_count: int = 0
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
        bar = last_row.to_dict()
        price = float(bar.get("close", 0.0))
        self._last_prices[symbol] = price

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
                self._record_trade(symbol, pos, price, ts)
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

        # Record equity and OHLCV after processing all actions
        self._record_equity(ts)
        if self._on_ohlcv:
            self._on_ohlcv(self._run_id, symbol, self._timeframe, bar, ts)

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

    def _calc_equity(self) -> float:
        """Total equity = cash + market value of all positions (per-symbol prices)."""
        cm = self._executor.cost_model
        mtm = 0.0
        for sym, pos in self._positions.items():
            price = self._last_prices.get(sym, pos.entry_price)
            mtm += price * pos.quantity * cm.multiplier
        return self._cash + mtm

    def _record_equity(self, ts: datetime) -> None:
        """Calculate equity + drawdown and call on_bar callback."""
        equity = self._calc_equity()
        self._equity_peak = max(self._equity_peak, equity)
        drawdown = (equity - self._equity_peak) / self._equity_peak if self._equity_peak > 0 else 0.0
        ret_1d = (equity / self._prev_equity - 1.0) if self._prev_equity > 0 else 0.0
        self._prev_equity = equity

        if self._on_bar:
            self._on_bar(self._run_id, ts, equity, drawdown, ret_1d)

    def _record_trade(self, symbol: str, pos: Position, exit_price: float, ts: datetime) -> None:
        """Calculate trade PnL using cost model and call on_trade callback."""
        cm = self._executor.cost_model
        direction = -1.0 if pos.side == "short" else 1.0
        gross_pnl = (exit_price - pos.entry_price) * pos.quantity * cm.multiplier * direction
        commission = cm.calc_commission(exit_price, pos.quantity) + cm.calc_commission(pos.entry_price, pos.quantity)
        slippage = cm.calc_slippage(pos.quantity) * 2
        net_pnl = gross_pnl - commission - slippage

        entry_notional = pos.entry_price * pos.quantity * cm.multiplier
        gross_return = (gross_pnl / entry_notional * 100) if entry_notional > 0 else 0.0
        net_return = (net_pnl / entry_notional * 100) if entry_notional > 0 else 0.0

        self._trade_count += 1
        trade_id = f"{self._run_id}-t{self._trade_count:04d}"

        if self._on_trade:
            self._on_trade({
                "run_id": self._run_id,
                "trade_id": trade_id,
                "entry_ts": pos.entry_ts,
                "exit_ts": ts,
                "symbol": symbol,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "quantity": pos.quantity,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                "gross_return": gross_return,
                "net_return": net_return,
                "holding_bars": self._bars_held.get(symbol, 0),
                "commission": commission,
                "slippage": slippage,
            })
