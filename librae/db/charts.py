"""TimescaleDB-backed adapters for Librae's pure trade chart renderer."""

from __future__ import annotations

import pandas as pd

from librae.backtest.charts import plot_kbars
from librae.backtest.schema import OrderEventRecord
from librae.db.timescale_reader import load_ohlcv, load_trade_events


def df_to_order_events(df: pd.DataFrame) -> list[OrderEventRecord]:
    """Convert ``load_trade_events()`` rows to canonical order-event records."""
    records = df.rename(columns={"_time": "ts"}).to_dict(orient="records")
    return [OrderEventRecord(**record) for record in records]


def plot_trades_by_run_id(
    run_id: str,
    *,
    symbol: str | None = None,
    block: bool = True,
):
    """Render one persisted run without rerunning its strategy."""
    ohlcv = load_ohlcv(run_id=run_id).set_index("_time")
    order_events = df_to_order_events(load_trade_events(run_id))
    resolved_symbol = symbol or (
        order_events[0].symbol if order_events else ohlcv["symbol"].iloc[0]
    )
    return plot_kbars(ohlcv, order_events, resolved_symbol, block=block)
