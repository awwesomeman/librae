"""Trade chart rendering — lightweight-charts overlay of order_events on OHLCV.

Pure rendering only: consumes already-computed BacktestOutput.order_events
and never recomputes fills/PnL, so the chart can never drift from
compute_all()'s numbers (the sole source of truth, also written to
strategy_performance/trade_events for any downstream dashboard).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from librae.backtest.schema import OrderEventRecord

_ENTRY_EVENTS = {"open", "add"}
_EXIT_EVENTS = {"close", "reduce"}

# Candles stay low-saturation so they read as background structure; markers
# use high-saturation, hue-distinct colors so they always pop regardless of
# which way the candle under them is colored — the two must never share a
# hue family, or a marker disappears into a same-colored candle.
_BG_COLOR = "#131722"
_TEXT_COLOR = "#d1d4dc"
_GRID_COLOR = "rgba(255, 255, 255, 0.06)"
_CANDLE_UP = "#4c7a72"
_CANDLE_DOWN = "#8a5b5e"
_VOLUME_UP = "rgba(76, 122, 114, 0.5)"
_VOLUME_DOWN = "rgba(138, 91, 94, 0.5)"
_LONG_COLOR = "#00e5ff"
_SHORT_COLOR = "#ff9100"


def _to_utc(ts) -> pd.Timestamp:
    """Normalize to a tz-aware UTC Timestamp (naive input assumed already UTC).

    lightweight_charts computes candle epochs via `.astype('int64') // 10**9`
    (hardcodes nanosecond resolution) but marker epochs via `Timestamp.timestamp()`
    (resolution-agnostic, but naive input is treated as *local* system time) —
    two different formulas. pandas>=2 can parse plain strings into coarser-than-ns
    datetime64 (e.g. microsecond), which silently breaks the first formula, and a
    naive timestamp breaks the second formula's alignment with it on any machine
    not running in UTC. Passing explicit tz-aware UTC Timestamps (never strings)
    for both candles and markers, each forced through the exact conversion this
    module controls, is what keeps them landing on the same epoch.
    """
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _build_markers(order_events: Sequence[OrderEventRecord], symbol: str) -> list[dict]:
    """Convert one symbol's OrderEventRecords into lightweight-charts markers.

    entry = open/add (covers scale-ins), exit = close/reduce (covers partial
    closes) — using OrderEvent instead of TradeResult avoids duplicate entry
    markers when a position is closed in multiple tranches.
    """
    markers = []
    for ev in order_events:
        if ev.symbol != symbol:
            continue
        is_long = ev.side == "long"
        time = _to_utc(ev.ts)

        if ev.event_type in _ENTRY_EVENTS:
            markers.append(
                {
                    "time": time,
                    "position": "below" if is_long else "above",
                    "shape": "arrow_up" if is_long else "arrow_down",
                    "color": _LONG_COLOR if is_long else _SHORT_COLOR,
                    "text": f"{'BUY' if is_long else 'SELL'} @ {ev.price:.4g}",
                }
            )
        elif ev.event_type in _EXIT_EVENTS:
            ret = f" ({ev.net_return:+.1f}%)" if ev.net_return is not None else ""
            markers.append(
                {
                    "time": time,
                    "position": "above" if is_long else "below",
                    "shape": "arrow_down" if is_long else "arrow_up",
                    "color": _SHORT_COLOR if is_long else _LONG_COLOR,
                    "text": f"EXIT @ {ev.price:.4g}{ret}",
                }
            )

    markers.sort(key=lambda m: m["time"])
    return markers


def _prepare_ohlcv(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Single-symbol OHLCV (DatetimeIndex) -> lightweight-charts input frame.

    Builds the 'time' column as explicit datetime64[ns] UTC-wall-clock — see
    _to_utc() for why (pandas>=2 can otherwise hand the library a coarser
    resolution than its hardcoded ns-epoch math expects).
    """
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in ohlcv.columns]
    df = ohlcv[cols].copy()
    idx = pd.DatetimeIndex(ohlcv.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.insert(0, "time", idx.tz_localize(None).astype("datetime64[ns]"))
    return df.reset_index(drop=True)


def plot_trades(
    ohlcv: pd.DataFrame,
    order_events: Sequence[OrderEventRecord],
    symbol: str,
    *,
    block: bool = True,
):
    """Render OHLCV candles with entry/exit markers for one symbol.

    Args:
        ohlcv: Single-symbol OHLCV DataFrame with DatetimeIndex, already
            sliced for `symbol` (e.g. df.xs(symbol, level="symbol")).
        order_events: BacktestOutput.order_events — filtered here by symbol,
            so the full (possibly multi-symbol) list can be passed as-is.
        symbol: Which symbol's events to overlay.
        block: Passed to Chart.show() — False for scripted/headless use.
    """
    from lightweight_charts import Chart

    chart = Chart(title=f"{symbol} Order Events")
    chart.layout(background_color=_BG_COLOR, text_color=_TEXT_COLOR)
    chart.grid(color=_GRID_COLOR)
    chart.candle_style(
        up_color=_CANDLE_UP,
        down_color=_CANDLE_DOWN,
        wick_up_color=_CANDLE_UP,
        wick_down_color=_CANDLE_DOWN,
        border_up_color=_CANDLE_UP,
        border_down_color=_CANDLE_DOWN,
    )
    chart.volume_config(up_color=_VOLUME_UP, down_color=_VOLUME_DOWN)  # must precede set()
    chart.set(_prepare_ohlcv(ohlcv))
    for m in _build_markers(order_events, symbol):
        chart.marker(
            time=m["time"],
            position=m["position"],
            shape=m["shape"],
            color=m["color"],
            text=m["text"],
        )
    chart.show(block=block)
    return chart


def _df_to_order_events(df: pd.DataFrame) -> list[OrderEventRecord]:
    """load_trade_events() output (``_time`` column) -> OrderEventRecord list.

    Reuses the same schema type as an in-memory run so _build_markers has a
    single input shape regardless of whether the caller just ran a backtest
    or is loading a past one — no separate DB-row marker logic to drift.
    """
    from librae.backtest.schema import OrderEventRecord

    records = df.rename(columns={"_time": "ts"}).to_dict(orient="records")
    return [OrderEventRecord(**r) for r in records]


def plot_trades_by_run_id(run_id: str, *, symbol: str | None = None, block: bool = True):
    """Open a persisted run's trade chart straight from TimescaleDB.

    No backtest re-run needed: reads the exact order_events/OHLCV that
    build_output()/db.timescale_writer already wrote for this run_id, so the
    chart can't drift from what's in strategy_performance/any downstream dashboard.

    symbol defaults to the run's primary symbol (backtest_runs.symbol).
    """
    from db.timescale_reader import load_ohlcv, load_trade_events

    ohlcv = load_ohlcv(run_id=run_id).set_index("_time")
    order_events = _df_to_order_events(load_trade_events(run_id))
    resolved_symbol = symbol or (
        order_events[0].symbol if order_events else ohlcv["symbol"].iloc[0]
    )
    return plot_trades(ohlcv, order_events, resolved_symbol, block=block)
