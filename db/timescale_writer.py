"""TimescaleDB writer — low-level writes and high-level save helpers.

Naming convention:
    write_*  — single-table write, no data transformation
    save_*   — multi-table orchestrator, may extract/transform data

Tables: backtest_runs, equity_curve, trade_events,
strategy_performance, ohlcv, signal_events.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import psycopg2
import psycopg2.extras

if TYPE_CHECKING:
    from librae.core.run_config import RunConfig
    from psycopg2.extensions import cursor as PgCursor

import pandas as pd

from librae.backtest.schema import BacktestOutput
from librae.core.utils import to_canonical
from db import TIMESCALE_DSN, get_conn

logger = logging.getLogger(__name__)


def _to_dt(ts: Any) -> datetime | None:
    """Normalise timestamp to tz-aware UTC datetime.

    Raises ValueError if a timezone-naive datetime is provided — callers
    must supply tz-aware timestamps so the DB stores correct UTC values.
    """
    if ts is None:
        return None
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            raise ValueError(
                f"Timezone-naive datetime {ts!r} — "
                "provide tz-aware timestamps (e.g. with tzinfo=timezone.utc)"
            )
        return ts.astimezone(timezone.utc)
    return ts


def _extract_signals(
    df: pd.DataFrame,
    symbol: str,
    signal_column: str = "entry_signal",
) -> tuple[pd.DataFrame, pd.Series]:
    """Extract symbol slice and non-zero signal values from a DataFrame.

    Returns (symbol_df, signal_series). Handles both MultiIndex and
    single-level DatetimeIndex inputs.
    """
    if isinstance(df.index, pd.MultiIndex):
        symbol_df = df.xs(symbol, level="symbol")
    else:
        symbol_df = df
    raw = symbol_df[signal_column].astype(float)
    signal_series = raw.dropna()
    signal_series = signal_series[signal_series != 0]
    return symbol_df, signal_series


def write_run_metadata(
    run_id: str,
    strategy: str,
    symbol: str,
    timeframe: str,
    mode: str,
    *,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
    run_ts: datetime | None = None,
    data_source: str | None = None,
    poll_seconds: int | None = None,
    params_json: dict | None = None,
    perf_params_json: dict | None = None,
    config_hash: str | None = None,
    cur: PgCursor | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single run record to backtest_runs (upsert).

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction).  Otherwise opens its own connection and commits.
    """
    sql = """INSERT INTO backtest_runs
               (run_id, strategy, symbol, timeframe, data_source,
                start_ts, end_ts, run_ts, mode, poll_seconds,
                params, perf_params, config_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                 strategy=EXCLUDED.strategy, run_ts=EXCLUDED.run_ts,
                 mode=EXCLUDED.mode, poll_seconds=EXCLUDED.poll_seconds,
                 params=EXCLUDED.params, perf_params=EXCLUDED.perf_params,
                 config_hash=EXCLUDED.config_hash"""
    params_val = json.dumps(params_json) if params_json is not None else None
    perf_val = json.dumps(perf_params_json) if perf_params_json is not None else None
    values = (
        run_id, strategy, symbol, timeframe, data_source,
        _to_dt(start_ts), _to_dt(end_ts),
        _to_dt(run_ts) or datetime.now(tz=timezone.utc),
        mode, poll_seconds,
        params_val, perf_val, config_hash,
    )
    if cur is not None:
        cur.execute(sql, values)
    else:
        with get_conn(dsn) as conn:
            c = conn.cursor()
            c.execute(sql, values)
            c.close()


def _update_perf_params(
    run_id: str,
    perf_params: dict[str, Any],
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Update perf_params for an existing run (lightweight recompute path)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE backtest_runs SET perf_params = %s WHERE run_id = %s",
            (json.dumps(perf_params), run_id),
        )
        cur.close()


def update_heartbeat(run_id: str, dsn: str = TIMESCALE_DSN) -> None:
    """Update last_heartbeat timestamp for a running sim/live process."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE backtest_runs SET last_heartbeat = NOW() WHERE run_id = %s",
            (run_id,),
        )
        cur.close()


def write_backtest_output(
    output: BacktestOutput,
    *,
    signal_series: pd.Series | None = None,
    params: dict | None = None,
    perf_params: dict | None = None,
    config_hash: str | None = None,
    dsn: str = TIMESCALE_DSN,
) -> dict:
    """Write a complete BacktestOutput to TimescaleDB.

    # TODO: add ON CONFLICT (config_hash) DO NOTHING as defensive safety net
    # for multi-worker race conditions. Currently relies on UNIQUE INDEX
    # raising a postgres error, which works but is less clean.
    # For --force path, use ON CONFLICT (config_hash) DO UPDATE.

    Args:
        output: BacktestOutput to write.
        signal_series: Optional Series (index=timestamp, values=signal_value)
            to write to signal_events. Only non-NaN values are written.
        params: Optional strategy parameters dict to store as JSONB.
        perf_params: Optional perf parameters dict to store as JSONB.
        config_hash: Optional deterministic hash for dedup.
        dsn: TimescaleDB DSN.

    Returns:
        Dict mapping table names to row counts written.
    """
    meta = output.run_metadata
    m = output.metrics
    counts: dict[str, int] = {}

    # WHY: single transaction for atomicity — partial writes on failure
    # would leave DB in inconsistent state (e.g. trades without metadata).
    with get_conn(dsn) as conn:
        cur = conn.cursor()

        write_run_metadata(
            run_id=meta.run_id, strategy=meta.strategy, symbol=meta.symbol,
            timeframe=meta.timeframe, mode=meta.mode,
            start_ts=meta.start_ts, end_ts=meta.end_ts, run_ts=meta.run_ts,
            data_source=meta.data_source,
            params_json=params,
            perf_params_json=perf_params,
            config_hash=config_hash,
            cur=cur,
        )
        counts["backtest_runs"] = 1

        tf = to_canonical(meta.timeframe)

        # Clear old data (idempotent re-run)
        cur.execute("DELETE FROM equity_curve WHERE run_id = %s", (meta.run_id,))
        cur.execute("DELETE FROM trade_events WHERE run_id = %s", (meta.run_id,))
        cur.execute("DELETE FROM strategy_performance WHERE run_id = %s", (meta.run_id,))

        # equity_curve (batch)
        if output.equity_curve:
            eq_rows = [
                (
                    _to_dt(eq.ts), meta.run_id, eq.equity,
                    eq.benchmark_equity, eq.drawdown,
                    eq.ret_1d, eq.benchmark_ret_1d,
                    meta.strategy,
                )
                for eq in output.equity_curve
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO equity_curve
                   (ts, run_id, equity, benchmark_equity, drawdown, ret_1d,
                    benchmark_ret_1d, strategy)
                   VALUES %s""",
                eq_rows,
                page_size=1000,
            )
            counts["equity_curve"] = len(eq_rows)

        # trade_events (batch)
        if output.order_events:
            event_rows = [
                (
                    ev.event_id, meta.run_id,
                    meta.strategy, meta.mode, tf,
                    _to_dt(ev.ts),
                    ev.symbol, ev.side, ev.event_type,
                    ev.quantity, ev.price, ev.entry_price,
                    ev.position_quantity, ev.notional,
                    ev.commission, ev.slippage, ev.tax,
                    ev.pnl, ev.net_return,
                    _to_dt(ev.entry_ts) if ev.entry_ts else None,
                    ev.holding_periods, ev.reason,
                )
                for ev in output.order_events
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO trade_events
                   (event_id, run_id,
                    strategy, mode, timeframe,
                    ts, symbol, side, event_type,
                    quantity, price, entry_price,
                    position_quantity, notional,
                    commission, slippage, tax,
                    pnl, net_return,
                    entry_ts, holding_periods, reason)
                   VALUES %s
                   ON CONFLICT (event_id, ts) DO NOTHING""",
                event_rows,
                page_size=500,
            )
            counts["trade_events"] = len(event_rows)

        # signal_events (from feature-layer signal_series)
        if signal_series is not None and not signal_series.empty:
            cur.execute(
                """DELETE FROM signal_events
                   WHERE strategy = %s AND symbol = %s AND mode = 'backtest'
                     AND timeframe = %s
                     AND ts BETWEEN %s AND %s""",
                (meta.strategy, meta.symbol, tf,
                 _to_dt(meta.start_ts), _to_dt(meta.end_ts)),
            )
            so_rows = [
                (_to_dt(ts), meta.run_id, meta.strategy, meta.symbol, "backtest",
                 tf, float(val), None)
                for ts, val in signal_series.items()
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO signal_events
                   (ts, run_id, strategy, symbol, mode, timeframe,
                    signal_value, price)
                   VALUES %s
                   ON CONFLICT (ts, strategy, symbol, mode, timeframe)
                   DO NOTHING""",
                so_rows,
                page_size=1000,
            )
            counts["signal_events"] = len(so_rows)

        write_performance(meta.run_id, m, cur=cur)
        counts["strategy_performance"] = 1

        cur.close()

    return counts


def write_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    data_source: str,
    dsn: str = TIMESCALE_DSN,
) -> int:
    """Write OHLCV DataFrame to TimescaleDB ohlcv table.

    Expects df with DatetimeIndex (or 'ts'/'timestamp' column) and
    columns: open, high, low, close, volume.
    Returns number of rows written.
    """
    if df is None or df.empty:
        return 0

    timeframe = to_canonical(timeframe)

    # Normalise index -> ts column (reset_index returns a new DataFrame)
    if "ts" not in df.columns and "timestamp" not in df.columns:
        df = df.reset_index()
    ts_col = "ts" if "ts" in df.columns else "timestamp"

    # Ensure tz-aware UTC — reject naive timestamps early
    ts_series = pd.to_datetime(df[ts_col])
    if ts_series.dt.tz is None:
        raise ValueError(
            "OHLCV timestamps are timezone-naive — "
            "fetcher must provide tz-aware datetimes "
            "(e.g. pd.to_datetime(..., utc=True))"
        )
    ts_utc = ts_series.dt.tz_convert("UTC")

    rows = list(zip(
        ts_utc.apply(_to_dt),
        [symbol] * len(df),
        [timeframe] * len(df),
        [data_source] * len(df),
        df["open"].astype(float),
        df["high"].astype(float),
        df["low"].astype(float),
        df["close"].astype(float),
        df.get("volume", pd.Series([0.0] * len(df))).astype(float),
    ))

    with get_conn(dsn) as conn:
        cur = conn.cursor()
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO ohlcv (ts, symbol, timeframe, data_source,
               open, high, low, close, volume)
               VALUES %s
               ON CONFLICT (ts, symbol, timeframe, data_source) DO NOTHING""",
            rows,
            page_size=2000,
        )
        cur.close()

    return len(rows)


def write_signal_event(
    ts: datetime,
    run_id: str,
    strategy: str,
    symbol: str,
    mode: str,
    timeframe: str,
    signal_value: float,
    price: float | None = None,
    *,
    cur: PgCursor | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single signal event row (upsert, idempotent).

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction).  Otherwise opens its own connection and commits.
    """
    sql = """INSERT INTO signal_events
               (ts, run_id, strategy, symbol, mode, timeframe, signal_value, price)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (ts, strategy, symbol, mode, timeframe)
               DO NOTHING"""
    values = (_to_dt(ts), run_id, strategy, symbol, mode, timeframe,
              signal_value, price)
    if cur is not None:
        cur.execute(sql, values)
    else:
        with get_conn(dsn) as conn:
            c = conn.cursor()
            c.execute(sql, values)
            c.close()


def write_equity_point(
    ts: datetime,
    run_id: str,
    equity: float,
    drawdown: float = 0.0,
    ret_1d: float = 0.0,
    benchmark_equity: float | None = None,
    benchmark_ret_1d: float | None = None,
    strategy: str | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single equity curve point (upsert by ts + run_id)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO equity_curve
               (ts, run_id, equity, benchmark_equity, drawdown, ret_1d,
                benchmark_ret_1d, strategy)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, ts) DO UPDATE SET
                 equity=EXCLUDED.equity, drawdown=EXCLUDED.drawdown,
                 ret_1d=EXCLUDED.ret_1d, strategy=EXCLUDED.strategy""",
            (
                _to_dt(ts), run_id, equity, benchmark_equity,
                drawdown, ret_1d, benchmark_ret_1d, strategy,
            ),
        )
        cur.close()


def write_trade_event(
    event_id: str,
    run_id: str,
    strategy: str,
    mode: str,
    timeframe: str,
    ts: datetime,
    symbol: str,
    side: str,
    event_type: str,
    quantity: float,
    price: float,
    entry_price: float,
    position_quantity: float,
    notional: float,
    commission: float = 0.0,
    slippage: float = 0.0,
    tax: float = 0.0,
    pnl: float | None = None,
    net_return: float | None = None,
    entry_ts: datetime | None = None,
    holding_periods: int | None = None,
    reason: str = "",
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single trade event (upsert by event_id + ts)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO trade_events
               (event_id, run_id,
                strategy, mode, timeframe,
                ts, symbol, side, event_type,
                quantity, price, entry_price,
                position_quantity, notional,
                commission, slippage, tax,
                pnl, net_return,
                entry_ts, holding_periods, reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (event_id, ts) DO NOTHING""",
            (
                event_id, run_id,
                strategy, mode, timeframe,
                _to_dt(ts), symbol, side, event_type,
                quantity, price, entry_price,
                position_quantity, notional,
                commission, slippage, tax,
                pnl, net_return,
                _to_dt(entry_ts) if entry_ts else None,
                holding_periods, reason,
            ),
        )
        cur.close()


def write_performance(
    run_id: str,
    metrics: Any,
    *,
    cur: PgCursor | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write/update strategy_performance for a run_id.

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction).  Otherwise opens its own connection and commits.
    """
    sql = """INSERT INTO strategy_performance
               (run_id, total_return, annual_return, sharpe, sortino, calmar,
                max_drawdown, win_rate, profit_factor, trades,
                avg_trade_return, exposure_ratio, benchmark_return,
                total_commission, total_slippage, total_tax)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                 total_return=EXCLUDED.total_return,
                 annual_return=EXCLUDED.annual_return,
                 sharpe=EXCLUDED.sharpe,
                 sortino=EXCLUDED.sortino,
                 calmar=EXCLUDED.calmar,
                 max_drawdown=EXCLUDED.max_drawdown,
                 win_rate=EXCLUDED.win_rate,
                 profit_factor=EXCLUDED.profit_factor,
                 trades=EXCLUDED.trades,
                 avg_trade_return=EXCLUDED.avg_trade_return,
                 exposure_ratio=EXCLUDED.exposure_ratio,
                 benchmark_return=EXCLUDED.benchmark_return,
                 total_commission=EXCLUDED.total_commission,
                 total_slippage=EXCLUDED.total_slippage,
                 total_tax=EXCLUDED.total_tax"""
    params = (
        run_id, metrics.total_return, metrics.annual_return,
        metrics.sharpe, metrics.sortino, metrics.calmar,
        metrics.max_drawdown, metrics.win_rate, metrics.profit_factor,
        metrics.trades, metrics.avg_trade_return, metrics.exposure_ratio,
        metrics.benchmark_return, metrics.total_commission, metrics.total_slippage,
        metrics.total_tax,
    )
    if cur is not None:
        cur.execute(sql, params)
    else:
        with get_conn(dsn) as conn:
            c = conn.cursor()
            c.execute(sql, params)
            c.close()


def refresh_performance(
    run_id: str,
    cfg: RunConfig | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Recompute KPI metrics from DB equity_curve + trade_events and upsert.

    Called after each trade close in sim mode to keep Grafana KPIs up to date.
    When cfg is provided, uses cfg.perf_params for annualization settings.
    """
    from types import SimpleNamespace as _NS

    from db.timescale_reader import load_equity_curve, load_trade_events
    _CLOSE_TYPES = ["close", "reduce"]
    from librae.core.metrics import compute_all

    eq_df = load_equity_curve(run_id)
    if eq_df.empty or len(eq_df) < 2:
        return

    closed_df = load_trade_events(run_id, event_types=_CLOSE_TYPES)
    trade_rows = closed_df.to_dict("records") if not closed_df.empty else []

    # WHY: compute_all accepts primitive sequences — build them from DB rows
    eq_records = eq_df.to_dict("records")
    equity_values = [float(r["equity"]) for r in eq_records]
    timestamps = [r["_time"] for r in eq_records]
    trade_pnls = [
        _NS(
            gross_pnl=(r.get("pnl", 0) or 0) + (r.get("commission", 0) or 0)
                      + (r.get("slippage", 0) or 0) + (r.get("tax", 0) or 0),
            net_pnl=r.get("pnl", 0) or 0,
            commission=r.get("commission", 0) or 0,
            slippage=r.get("slippage", 0) or 0,
            tax=r.get("tax", 0) or 0,
            gross_return=0.0,
            net_return=r.get("net_return", 0.0) or 0.0,
            exit_commission=0.0, exit_slippage=0.0, exit_tax=0.0,
        )
        for r in trade_rows
    ]
    trade_quantities = [float(r.get("quantity", 1.0)) for r in trade_rows]

    perf_kwargs: dict[str, Any] = {}
    if cfg is not None:
        perf_kwargs = cfg.perf_params

    metrics = compute_all(
        equity_values=equity_values,
        timestamps=timestamps,
        trade_pnls=trade_pnls,
        total_periods=len(equity_values),
        trade_quantities=trade_quantities,
        **perf_kwargs,
    )
    write_performance(run_id, metrics, dsn=dsn)


def save_signal_results(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    strategy: str,
    data_source: str,
    run_id: str | None = None,
    mode: str = "backtest",
    signal_column: str = "entry_signal",
    cfg: RunConfig | None = None,
) -> dict:
    """Write signal history + OHLCV to DB. Independent of backtest engine.

    Use this for signal quality analysis without running a full backtest.
    When cfg is provided, stores config_hash and perf_params.
    """
    symbol_df, signal_series = _extract_signals(df, symbol, signal_column)
    tf = to_canonical(timeframe)
    counts: dict[str, int] = {}

    if not signal_series.empty:
        start_ts = signal_series.index.min()
        end_ts = signal_series.index.max()

        with get_conn() as conn:
            cur = conn.cursor()

            if run_id is not None:
                write_run_metadata(
                    run_id, strategy, symbol, tf, mode,
                    start_ts=_to_dt(start_ts), end_ts=_to_dt(end_ts),
                    data_source=data_source,
                    config_hash=cfg.config_hash if cfg else None,
                    perf_params_json=cfg.perf_params if cfg else None,
                    params_json=cfg.params if cfg else None,
                    cur=cur,
                )

            cur.execute(
                """DELETE FROM signal_events
                   WHERE strategy = %s AND symbol = %s AND mode = %s
                     AND timeframe = %s
                     AND ts BETWEEN %s AND %s""",
                (strategy, symbol, mode, tf,
                 _to_dt(start_ts), _to_dt(end_ts)),
            )
            so_rows = [
                (_to_dt(ts), run_id, strategy, symbol, mode, tf, float(val), None)
                for ts, val in signal_series.items()
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO signal_events
                   (ts, run_id, strategy, symbol, mode, timeframe,
                    signal_value, price)
                   VALUES %s
                   ON CONFLICT (ts, strategy, symbol, mode, timeframe)
                   DO NOTHING""",
                so_rows,
                page_size=1000,
            )
            counts["signal_events"] = len(so_rows)
            cur.close()

    ohlcv_df = symbol_df[["open", "high", "low", "close", "volume"]]
    ohlcv_df.index.name = "ts"
    counts["ohlcv"] = write_ohlcv(ohlcv_df, symbol, timeframe, data_source=data_source)
    return counts


def save_strategy_results(
    output: BacktestOutput,
    df: pd.DataFrame,
    cfg: RunConfig,
    signal_column: str = "entry_signal",
) -> dict:
    """Write strategy backtest results + signal history to DB.

    Writes: backtest_runs, equity_curve, trade_events, strategy_performance,
    signal_events, ohlcv.
    """
    symbol = cfg.symbol
    timeframe = cfg.timeframe
    data_source = cfg.data_source

    symbol_df, signal_series = _extract_signals(df, symbol, signal_column)
    counts = write_backtest_output(
        output,
        signal_series=signal_series,
        params=cfg.params,
        perf_params=cfg.perf_params,
        config_hash=cfg.config_hash,
    )

    ohlcv_df = symbol_df[["open", "high", "low", "close", "volume"]]
    ohlcv_df.index.name = "ts"
    counts["ohlcv"] = write_ohlcv(ohlcv_df, symbol, timeframe, data_source=data_source)
    return counts
