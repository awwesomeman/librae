"""Live/sim wiring — assembles LiveTrader with DB callbacks.

Encapsulates all the infrastructure concerns (DB writes, heartbeat, Telegram,
KPI refresh) so strategy run.py only provides strategy-specific pieces.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from librae.config.market_config import get_market
from librae.core.cost_model import CostModel
from librae.core.executor import TradeResult
from librae.core.strategy import Action, BaseStrategy
from librae.core.utils import generate_run_id, make_trade_id, to_ccxt
from librae.config.notification import TelegramConfig
from librae.notifications.telegram import TelegramAdapter, TelegramCredentials

from .engine import LiveTrader
from .executor import LiveExecutor

logger = logging.getLogger(__name__)


def build_live_trader(
    *,
    strategy: BaseStrategy,
    strategy_name: str,
    feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
    symbols: list[str],
    timeframe: str,
    market: str = "crypto",
    initial_balance: float = 100_000.0,
    poll_interval: int = 60,
    warmup_bars: int = 720,
    no_db: bool = False,
    telegram_config: dict | None = None,
    signal_column: str | None = None,
    params: dict | None = None,
) -> LiveTrader:
    """Build a fully wired LiveTrader for sim mode.

    Args:
        timeframe: Canonical label (e.g. "H1"). Converted to ccxt format internally.

    Handles: CostModel, Telegram, DB callbacks (signal, equity, trade, ohlcv,
    heartbeat, KPI refresh), run metadata registration.

    Returns a LiveTrader ready to call .run().
    """
    from brokers.crypto_adapter import CryptoAdapter
    from db.timescale_writer import (
        refresh_performance, update_heartbeat, write_equity_point,
        write_ohlcv, write_run_metadata,
        write_signal_outcome, write_trade,
    )

    if market != "crypto":
        raise ValueError(f"Unsupported market '{market}' — only 'crypto' is implemented")

    timeframe_ccxt = to_ccxt(timeframe)

    adapter = CryptoAdapter()
    tg_config = TelegramConfig.from_dict(telegram_config or {})
    tg_creds = TelegramCredentials.from_env("TELEGRAM")
    telegram = TelegramAdapter(config=tg_config, credentials=tg_creds)
    cost_model = CostModel.from_market(get_market(market))
    run_id = generate_run_id(f"{strategy_name}_{market}", symbols[0])

    if not no_db:
        try:
            write_run_metadata(
                run_id=run_id, strategy=strategy_name, symbol=symbols[0],
                timeframe=timeframe, mode="sim",
                start_ts=datetime.now(tz=timezone.utc),
                data_source="binance", poll_interval=poll_interval,
                params_json=params,
            )
        except Exception as e:
            logger.warning("DB write_run_metadata failed: %s", e)

    def _db_write(fn: Callable[..., object], *args: object, **kwargs: object) -> None:
        if no_db:
            return
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.warning("DB %s failed: %s", fn.__name__, e)

    def fetcher(symbol: str, tf_ccxt: str, limit: int, *, drop_incomplete: bool = False) -> pd.DataFrame:
        return adapter.fetch_ohlcv(symbol, tf_ccxt, limit, drop_incomplete=drop_incomplete)

    def warmup_fetcher(symbol: str, tf_ccxt: str, limit: int) -> pd.DataFrame:
        """DB-first warmup: reads historical bars from DB, fills gaps from API."""
        from data.ohlcv import get_ohlcv
        from librae.core.utils import interval_to_timedelta
        import math

        # WHY: compute months from limit + timeframe to avoid over-fetching
        bar_hours = interval_to_timedelta(tf_ccxt).total_seconds() / 3600
        months_needed = max(1, math.ceil(limit * bar_hours / 24 / 30))

        df = get_ohlcv(symbol=symbol, interval=tf_ccxt, months=months_needed)
        if df.empty:
            return fetcher(symbol, tf_ccxt, limit, drop_incomplete=True)
        if "timestamp" in df.columns and "ts" not in df.columns:
            df = df.rename(columns={"timestamp": "ts"})
        if len(df) > limit:
            df = df.iloc[-limit:]
        return df.reset_index(drop=True)

    def on_signal_outcome_cb(symbol: str, ts: datetime, signal_value: float, price: float) -> None:
        _db_write(
            write_signal_outcome,
            signal_ts=ts, strategy=strategy_name, symbol=symbol,
            mode="sim", timeframe=timeframe,
            signal_value=signal_value, price=price,
        )

    def on_bar(run_id_: str, ts: datetime, equity: float, drawdown: float, ret_1d: float) -> None:
        _db_write(
            write_equity_point, ts=ts, run_id=run_id_, equity=equity,
            drawdown=drawdown, ret_1d=ret_1d, strategy_name=strategy_name,
        )

    _trade_seq = 0

    def on_trade(trade: TradeResult) -> None:
        nonlocal _trade_seq
        _trade_seq += 1
        _db_write(
            write_trade,
            run_id=run_id,
            trade_id=make_trade_id(run_id, _trade_seq),
            entry_ts=trade.entry_ts,
            exit_ts=trade.exit_ts,
            symbol=trade.symbol,
            side=trade.side,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            quantity=trade.quantity,
            gross_pnl=trade.gross_pnl,
            net_pnl=trade.net_pnl,
            gross_return=trade.gross_return,
            net_return=trade.net_return,
            holding_bars=trade.holding_bars,
            commission=trade.commission,
            slippage=trade.slippage,
            tax=trade.tax,
        )
        _db_write(refresh_performance, run_id)

    def on_ohlcv(symbol: str, timeframe_: str, bar: dict[str, float], ts: datetime) -> None:
        row = pd.DataFrame([{
            "ts": ts,
            "open": bar.get("open", 0),
            "high": bar.get("high", 0),
            "low": bar.get("low", 0),
            "close": bar.get("close", 0),
            "volume": bar.get("volume", 0),
        }]).set_index("ts")
        _db_write(write_ohlcv, row, symbol, timeframe_, source="binance_spot")

    def on_heartbeat(run_id_: str) -> None:
        _db_write(update_heartbeat, run_id_)

    executor = LiveExecutor(
        cost_model, simulation=True, telegram=telegram,
        strategy_name=strategy_name,
    )

    return LiveTrader(
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
        on_bar=on_bar,
        on_trade=on_trade,
        on_ohlcv=on_ohlcv,
        on_heartbeat=on_heartbeat,
        on_signal_outcome=on_signal_outcome_cb if signal_column else None,
        signal_column=signal_column,
        warmup_fetcher=warmup_fetcher if not no_db else None,
    )
