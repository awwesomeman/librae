"""Sim mode wiring — assembles LiveRunner with DB callbacks.

Encapsulates all the infrastructure concerns (DB writes, heartbeat, Telegram,
KPI refresh) so strategy run.py only provides strategy-specific pieces.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from librae.config.market_config import get_market
from librae.core.cost_model import CostModel
from librae.core.strategy import Action, BaseStrategy
from librae.core.utils import generate_run_id
from librae.notifications.telegram import TelegramAdapter

from .engine import LiveRunner
from .executor import LiveExecutor

logger = logging.getLogger(__name__)


def build_sim_runner(
    *,
    strategy: BaseStrategy,
    strategy_name: str,
    feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
    symbols: list[str],
    timeframe_ccxt: str,
    timeframe_db: str,
    market: str = "crypto",
    initial_balance: float = 100_000.0,
    poll_interval: int = 60,
    warmup_bars: int = 720,
    no_db: bool = False,
) -> LiveRunner:
    """Build a fully wired LiveRunner for sim mode.

    Handles: CostModel, Telegram, DB callbacks (signal, equity, trade, ohlcv,
    heartbeat, KPI refresh), run metadata registration.

    Returns a LiveRunner ready to call .run().
    """
    from brokers.crypto_adapter import CryptoAdapter
    from db.timescale_writer import (
        refresh_performance, update_heartbeat, write_equity_point,
        write_ohlcv, write_run_metadata, write_signal, write_trade,
    )

    if market != "crypto":
        raise ValueError(f"Unsupported market '{market}' — only 'crypto' is implemented")
    adapter = CryptoAdapter()
    telegram = TelegramAdapter()
    cost_model = CostModel.from_market(get_market(market))
    run_id = generate_run_id(f"{strategy_name}_{market}", symbols[0])

    # Register run in DB
    if not no_db:
        try:
            write_run_metadata(
                run_id=run_id, strategy=strategy_name, symbol=symbols[0],
                timeframe=timeframe_db, mode="sim",
                start_ts=datetime.now(tz=timezone.utc),
                data_source="binance", poll_interval=poll_interval,
            )
        except Exception as e:
            logger.warning("DB write_run_metadata failed: %s", e)

    def _db_write(fn: Callable, *a: Any, **kw: Any) -> None:
        if no_db:
            return
        try:
            fn(*a, **kw)
        except Exception as e:
            logger.warning("DB %s failed: %s", fn.__name__, e)

    def fetcher(symbol: str, timeframe: str, limit: int, **kwargs: Any) -> pd.DataFrame:
        return adapter.fetch_ohlcv(symbol, timeframe, limit, **kwargs)

    def on_signal(symbol: str, action: Action, price: float, ts: datetime) -> None:
        signal_type = "entry" if action.type in ("buy", "sell") else "exit"
        strength = 1.0 if action.type == "buy" else (
            -1.0 if action.type == "sell" else 0.0
        )
        _db_write(
            write_signal,
            ts=ts, run_id=run_id, strategy=strategy_name,
            symbol=symbol, timeframe=timeframe_db, signal_type=signal_type,
            source="sim", price=price, signal_strength=strength,
        )

    def on_bar(rid: str, ts: datetime, equity: float, drawdown: float, ret_1d: float) -> None:
        _db_write(write_equity_point, ts=ts, run_id=rid, equity=equity, drawdown=drawdown, ret_1d=ret_1d)

    def on_trade(trade: dict) -> None:
        _db_write(write_trade, **trade)
        _db_write(refresh_performance, run_id)

    def on_ohlcv(rid: str, symbol: str, timeframe: str, bar: dict, ts: datetime) -> None:
        row = pd.DataFrame([{
            "ts": ts,
            "open": bar.get("open", 0),
            "high": bar.get("high", 0),
            "low": bar.get("low", 0),
            "close": bar.get("close", 0),
            "volume": bar.get("volume", 0),
        }]).set_index("ts")
        _db_write(write_ohlcv, row, symbol, timeframe, rid, source="sim")

    def on_heartbeat(rid: str) -> None:
        _db_write(update_heartbeat, rid)

    executor = LiveExecutor(
        cost_model, simulation=True, telegram=telegram,
        strategy_name=strategy_name,
    )

    runner = LiveRunner(
        strategy=strategy,
        symbols=symbols,
        fetcher=fetcher,
        feature_fn=feature_fn,
        executor=executor,
        run_id=run_id,
        timeframe=timeframe_ccxt,
        warmup_bars=warmup_bars,
        initial_balance=initial_balance,
        poll_interval=poll_interval,
        on_signal=on_signal,
        on_bar=on_bar,
        on_trade=on_trade,
        on_ohlcv=on_ohlcv,
        on_heartbeat=on_heartbeat,
    )

    return runner
