"""TimescaleDB-backed adapters for Librae's pure trade chart renderer."""

from __future__ import annotations

import pandas as pd

from librae.backtest.charts import plot_kbars
from librae.backtest.schema import PositionEventRecord
from librae.db.timescale_reader import load_ohlcv, load_position_events


def df_to_position_events(df: pd.DataFrame) -> list[PositionEventRecord]:
    """Convert ``load_position_events()`` rows to canonical position-event records."""
    records = df.rename(columns={"_time": "ts"}).to_dict(orient="records")
    return [PositionEventRecord(**record) for record in records]


def plot_trades_by_run_id(
    run_id: str,
    *,
    symbol: str | None = None,
    block: bool = True,
):
    """Render one persisted run without rerunning its strategy."""
    ohlcv = load_ohlcv(run_id=run_id).set_index("_time")
    position_events = df_to_position_events(load_position_events(run_id))
    resolved_symbol = symbol or (
        position_events[0].symbol if position_events else ohlcv["symbol"].iloc[0]
    )
    return plot_kbars(ohlcv, position_events, resolved_symbol, block=block)
