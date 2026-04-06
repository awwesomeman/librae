"""SignalPoller — lightweight signal sim poller.

Replaces the LiveTrader + HoldStrategy hack for signal monitoring.
Only does: poll -> feature_fn -> extract signal -> write signal_events + ohlcv + heartbeat.
Does NOT do: strategy decisions, position management, equity curve, trade_events, Telegram.

Stateless constraint: no account/position state. The feature_fn must be
portfolio-agnostic (pure feature engineering, no position dependency).
"""
from __future__ import annotations

import logging
import signal
import time
import types
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

import pandas as pd

if TYPE_CHECKING:
    from librae.core.run_config import RunConfig

logger = logging.getLogger(__name__)


class SignalPoller:
    """Lightweight poller for signal-only sim/monitoring.

    Args:
        feature_fn: Callable(df: DataFrame) -> DataFrame with entry_signal column.
        cfg: RunConfig — configuration source.
    """

    def __init__(
        self,
        feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
        *,
        cfg: RunConfig,
    ) -> None:
        from brokers.crypto_adapter import CryptoAdapter
        from librae.core.utils import generate_run_id, to_ccxt

        self._feature_fn = feature_fn
        self._cfg = cfg
        self._symbols = cfg.symbols
        self._timeframe = to_ccxt(cfg.timeframe)
        self._poll_seconds = cfg.poll_seconds
        self._warmup_periods = (cfg.params or {}).get("warmup_periods", 200)

        adapter = CryptoAdapter()
        self._fetcher = lambda symbol, tf, limit, *, drop_incomplete=False: (
            adapter.fetch_ohlcv(symbol, tf, limit, drop_incomplete=drop_incomplete)
        )

        self._run_id = generate_run_id(cfg.strategy_name, cfg.symbol, cfg.timeframe)

        if not cfg.no_db:
            self._register_run()

        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._last_bar_ts: dict[str, datetime] = {}
        self._running: bool = False

    def _db_write(self, fn: Callable[..., object], *args: object, **kwargs: object) -> None:
        """Fire-and-forget DB write — never let DB errors block the poll loop."""
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.warning("DB %s failed: %s", fn.__name__, e)

    def _register_run(self) -> None:
        from db.timescale_writer import write_run_metadata
        try:
            write_run_metadata(
                run_id=self._run_id,
                strategy=self._cfg.strategy_name,
                symbol=self._cfg.symbol,
                timeframe=self._cfg.timeframe,
                mode=self._cfg.mode,
                start_ts=datetime.now(tz=timezone.utc),
                data_source=self._cfg.data_source,
                poll_seconds=self._cfg.poll_seconds,
                params_json=self._cfg.params,
                perf_params_json=self._cfg.perf_params,
                config_hash=self._cfg.config_hash,
            )
        except Exception as e:
            logger.warning("DB write_run_metadata failed: %s", e)

    def run(self, max_iterations: int | None = None) -> None:
        """Start the polling loop. Blocks until stopped or max_iterations reached."""
        self._running = True
        self._setup_signal_handlers()
        iteration = 0

        logger.info(
            "SignalPoller started: signal=%s, symbols=%s, timeframe=%s, poll=%ss",
            self._cfg.strategy_name, self._symbols, self._timeframe, self._poll_seconds,
        )

        try:
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
                    time.sleep(self._poll_seconds)
        except Exception:
            logger.exception("SignalPoller crashed")
        finally:
            logger.info("SignalPoller stopped")

    def stop(self) -> None:
        """Signal the poller to stop after the current cycle."""
        self._running = False

    def _setup_signal_handlers(self) -> None:
        def _handler(signum: int, frame: types.FrameType | None) -> None:
            logger.info("Received signal %d, shutting down gracefully", signum)
            self.stop()
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def _poll_cycle(self) -> None:
        """Single poll cycle: fetch, detect new bars, extract signals."""
        if not self._cfg.no_db:
            self._heartbeat()

        for symbol in self._symbols:
            df = self._fetch_with_cache(symbol)
            if df is None or df.empty:
                continue

            latest_ts = df["ts"].iloc[-1].to_pydatetime()
            prev_ts = self._last_bar_ts.get(symbol)

            if prev_ts is not None and latest_ts <= prev_ts:
                continue

            self._last_bar_ts[symbol] = latest_ts
            logger.info("New bar detected: %s @ %s", symbol, latest_ts)

            self._process_bar(symbol, df, latest_ts)

    def _fetch_with_cache(self, symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV with caching. Full fetch on first call, incremental after."""
        try:
            if symbol not in self._ohlcv_cache:
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

    def _process_bar(self, symbol: str, raw_df: pd.DataFrame, ts: datetime) -> None:
        """Run feature pipeline, extract signal, write to DB."""
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

        sig = bar.get("entry_signal")
        if sig is not None and not pd.isna(sig) and float(sig) != 0:
            logger.info("Signal detected: %s @ %s = %.4f (price=%.2f)",
                        symbol, ts, float(sig), price)
            if not self._cfg.no_db:
                self._write_signal(symbol, ts, float(sig), price)
        exit_sig = bar.get("exit_signal")
        if exit_sig is not None and not pd.isna(exit_sig) and float(exit_sig) != 0:
            logger.info("Exit signal: %s @ %s = %.4f (price=%.2f)",
                        symbol, ts, float(exit_sig), price)
            if not self._cfg.no_db:
                self._write_signal(symbol, ts, float(exit_sig), price, signal_type="exit")

        if not self._cfg.no_db:
            self._write_ohlcv(symbol, bar, ts)

    def _write_signal(
        self, symbol: str, ts: datetime, signal_value: float, price: float,
        signal_type: str = "entry",
    ) -> None:
        from db.timescale_writer import write_signal_event
        self._db_write(
            write_signal_event,
            ts=ts, run_id=self._run_id,
            strategy=self._cfg.strategy_name, symbol=symbol,
            mode=self._cfg.mode, timeframe=self._cfg.timeframe,
            signal_value=signal_value, price=price,
            signal_type=signal_type,
        )

    def _write_ohlcv(self, symbol: str, bar: dict, ts: datetime) -> None:
        from db.timescale_writer import write_ohlcv
        row = pd.DataFrame([{
            "ts": ts,
            "open": bar.get("open", 0),
            "high": bar.get("high", 0),
            "low": bar.get("low", 0),
            "close": bar.get("close", 0),
            "volume": bar.get("volume", 0),
        }]).set_index("ts")
        self._db_write(write_ohlcv, row, symbol, self._timeframe, data_source=self._cfg.data_source)

    def _heartbeat(self) -> None:
        from db.timescale_writer import update_heartbeat
        self._db_write(update_heartbeat, self._run_id)
