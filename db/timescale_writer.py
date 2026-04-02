"""TimescaleDB writer for BacktestOutput and live/sim signals.

Writes to tables: backtest_runs, equity_curve, trade_blotter,
strategy_performance, ohlcv, signal_outcomes.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import psycopg2
import psycopg2.extras

if TYPE_CHECKING:
    from psycopg2.extensions import cursor as PgCursor

import pandas as pd

from librae.backtest.schema import BacktestOutput
from librae.backtest.schema import SCHEMA_VERSION
from db import TIMESCALE_DSN, get_conn

logger = logging.getLogger(__name__)


def _to_dt(ts: Any) -> datetime | None:
    """Normalise timestamp to datetime (handles pandas Timestamp)."""
    if ts is None:
        return None
    if hasattr(ts, "to_pydatetime"):
        return ts.to_pydatetime()
    return ts


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
    data_source: str = "binance",
    sample: str | None = None,
    poll_interval: int | None = None,
    params_json: dict | None = None,
    cur: PgCursor | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single run record to backtest_runs (upsert).

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction).  Otherwise opens its own connection and commits.
    """
    sql = """INSERT INTO backtest_runs
               (run_id, strategy, symbol, timeframe, sample, data_source,
                start_ts, end_ts, run_ts, schema_version, mode, poll_interval,
                params)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                 strategy=EXCLUDED.strategy, run_ts=EXCLUDED.run_ts,
                 mode=EXCLUDED.mode, poll_interval=EXCLUDED.poll_interval,
                 params=EXCLUDED.params"""
    params_val = json.dumps(params_json) if params_json is not None else None
    values = (
        run_id, strategy, symbol, timeframe, sample, data_source,
        _to_dt(start_ts), _to_dt(end_ts),
        _to_dt(run_ts) or datetime.now(tz=timezone.utc),
        SCHEMA_VERSION, mode, poll_interval,
        params_val,
    )
    if cur is not None:
        cur.execute(sql, values)
    else:
        with get_conn(dsn) as conn:
            c = conn.cursor()
            c.execute(sql, values)
            c.close()


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
    dsn: str = TIMESCALE_DSN,
) -> dict:
    """Write a complete BacktestOutput to TimescaleDB.

    Args:
        output: BacktestOutput to write.
        signal_series: Optional Series (index=timestamp, values=signal_value)
            to write to signal_outcomes. Only non-NaN values are written.
        params: Optional strategy parameters dict to store as JSONB.
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
            timeframe=meta.timeframe, mode="backtest",
            start_ts=meta.start_ts, end_ts=meta.end_ts, run_ts=meta.run_ts,
            params_json=params,
            cur=cur,
        )
        counts["backtest_runs"] = 1

        # Clear old data (idempotent re-run)
        cur.execute("DELETE FROM equity_curve WHERE run_id = %s", (meta.run_id,))
        cur.execute("DELETE FROM trade_blotter WHERE run_id = %s", (meta.run_id,))
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
                    benchmark_ret_1d, strategy_name)
                   VALUES %s""",
                eq_rows,
                page_size=1000,
            )
            counts["equity_curve"] = len(eq_rows)

        # trade_blotter (batch)
        if output.trades:
            trade_rows = [
                (
                    tr.trade_id, meta.run_id,
                    _to_dt(tr.entry_ts), _to_dt(tr.exit_ts),
                    tr.symbol, tr.side,
                    tr.entry_price, tr.exit_price, tr.quantity,
                    tr.gross_pnl, tr.net_pnl,
                    tr.gross_return, tr.net_return,
                    tr.price_unit,
                    tr.quantity_unit,
                    tr.pnl_unit,
                    tr.commission, tr.slippage, tr.tax, tr.holding_bars,
                )
                for tr in output.trades
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO trade_blotter
                   (trade_id, run_id, entry_ts, exit_ts, symbol, side,
                    entry_price, exit_price, quantity,
                    gross_pnl, net_pnl,
                    gross_return, net_return,
                    price_unit, quantity_unit, pnl_unit,
                    commission, slippage, tax, holding_bars)
                   VALUES %s
                   ON CONFLICT (trade_id) DO NOTHING""",
                trade_rows,
                page_size=500,
            )
            counts["trade_blotter"] = len(trade_rows)

        # signal_outcomes (from feature-layer signal_series)
        if signal_series is not None and not signal_series.empty:
            # Idempotent re-run: clear signals within this backtest's time range
            cur.execute(
                """DELETE FROM signal_outcomes
                   WHERE strategy = %s AND symbol = %s AND source = 'backtest'
                     AND timeframe = %s
                     AND signal_ts BETWEEN %s AND %s""",
                (meta.strategy, meta.symbol, meta.timeframe,
                 _to_dt(meta.start_ts), _to_dt(meta.end_ts)),
            )
            so_rows = [
                (_to_dt(ts), meta.strategy, meta.symbol, "backtest",
                 meta.timeframe, float(val), None)
                for ts, val in signal_series.items()
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO signal_outcomes
                   (signal_ts, strategy, symbol, source, timeframe,
                    signal_value, price)
                   VALUES %s
                   ON CONFLICT (signal_ts, strategy, symbol, source, timeframe)
                   DO NOTHING""",
                so_rows,
                page_size=1000,
            )
            counts["signal_outcomes"] = len(so_rows)

        write_performance(meta.run_id, m, cur=cur)
        counts["strategy_performance"] = 1

        cur.close()

    return counts


def write_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    run_id: str | None = None,
    source: str = "backtest",
    dsn: str = TIMESCALE_DSN,
) -> int:
    """Write OHLCV DataFrame to TimescaleDB ohlcv table.

    Expects df with DatetimeIndex (or 'ts'/'timestamp' column) and
    columns: open, high, low, close, volume.
    Returns number of rows written.
    """
    if df is None or df.empty:
        return 0

    # Normalise index → ts column
    work = df.copy()
    if "ts" not in work.columns and "timestamp" not in work.columns:
        work = work.reset_index()
    ts_col = "ts" if "ts" in work.columns else "timestamp"

    rows = list(zip(
        work[ts_col].apply(_to_dt),
        [symbol] * len(work),
        [timeframe] * len(work),
        [run_id] * len(work),
        [source] * len(work),
        work["open"].astype(float),
        work["high"].astype(float),
        work["low"].astype(float),
        work["close"].astype(float),
        work.get("volume", pd.Series([0.0] * len(work))).astype(float),
    ))

    with get_conn(dsn) as conn:
        cur = conn.cursor()
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO ohlcv (ts, symbol, timeframe, run_id, source,
               open, high, low, close, volume)
               VALUES %s
               ON CONFLICT (ts, symbol, timeframe) DO NOTHING""",
            rows,
            page_size=2000,
        )
        cur.close()

    return len(rows)


def write_signal_outcome(
    signal_ts: datetime,
    strategy: str,
    symbol: str,
    source: str,
    timeframe: str,
    signal_value: float,
    price: float | None = None,
    *,
    cur: PgCursor | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single signal outcome row (upsert, idempotent).

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction).  Otherwise opens its own connection and commits.
    """
    sql = """INSERT INTO signal_outcomes
               (signal_ts, strategy, symbol, source, timeframe, signal_value, price)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (signal_ts, strategy, symbol, source, timeframe)
               DO NOTHING"""
    values = (_to_dt(signal_ts), strategy, symbol, source, timeframe,
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
    strategy_name: str | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single equity curve point (upsert by ts + run_id)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO equity_curve
               (ts, run_id, equity, benchmark_equity, drawdown, ret_1d,
                benchmark_ret_1d, strategy_name)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, ts) DO UPDATE SET
                 equity=EXCLUDED.equity, drawdown=EXCLUDED.drawdown,
                 ret_1d=EXCLUDED.ret_1d, strategy_name=EXCLUDED.strategy_name""",
            (
                _to_dt(ts), run_id, equity, benchmark_equity,
                drawdown, ret_1d, benchmark_ret_1d, strategy_name,
            ),
        )
        cur.close()


def write_trade(
    run_id: str,
    trade_id: str,
    entry_ts: datetime,
    exit_ts: datetime,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
    gross_pnl: float,
    net_pnl: float,
    gross_return: float,
    net_return: float,
    holding_bars: int,
    commission: float = 0.0,
    slippage: float = 0.0,
    tax: float = 0.0,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single trade to trade_blotter (upsert by trade_id)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO trade_blotter
               (trade_id, run_id, entry_ts, exit_ts, symbol, side,
                entry_price, exit_price, quantity,
                gross_pnl, net_pnl, gross_return, net_return,
                commission, slippage, tax, holding_bars)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (trade_id) DO NOTHING""",
            (
                trade_id, run_id, _to_dt(entry_ts), _to_dt(exit_ts),
                symbol, side, entry_price, exit_price, quantity,
                gross_pnl, net_pnl, gross_return, net_return,
                commission, slippage, tax, holding_bars,
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


def refresh_performance(run_id: str, dsn: str = TIMESCALE_DSN) -> None:
    """Recompute KPI metrics from DB equity_curve + trade_blotter and upsert.

    Called after each trade close in sim mode to keep Grafana KPIs up to date.
    """
    from types import SimpleNamespace as _NS

    from db.timescale_reader import load_equity_curve, load_trade_blotter
    from librae.core.metrics import compute_all

    eq_df = load_equity_curve(run_id)
    if eq_df.empty or len(eq_df) < 2:
        return

    trades_df = load_trade_blotter(run_id)
    trade_rows = trades_df.to_dict("records") if not trades_df.empty else []

    # WHY: compute_all accepts primitive sequences — build them from DB rows
    eq_records = eq_df.to_dict("records")
    equity_values = [float(r["equity"]) for r in eq_records]
    timestamps = [r["_time"] for r in eq_records]
    trade_pnls = [
        _NS(
            gross_pnl=r.get("gross_pnl", 0), net_pnl=r.get("net_pnl", 0),
            commission=r.get("commission", 0), slippage=r.get("slippage", 0),
            tax=r.get("tax", 0), gross_return=r.get("gross_return", 0.0),
            net_return=r.get("net_return", 0.0),
            exit_commission=0.0, exit_slippage=0.0, exit_tax=0.0,
        )
        for r in trade_rows
    ]
    holding_bars = [r.get("holding_bars", 0) for r in trade_rows]

    metrics = compute_all(
        equity_values=equity_values,
        timestamps=timestamps,
        trade_pnls=trade_pnls,
        total_bars=len(equity_values),
        holding_bars=holding_bars,
    )
    write_performance(run_id, metrics, dsn=dsn)


def persist_backtest(
    output: BacktestOutput,
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    params: dict | None = None,
    signal_column: str = "entry_signal",
) -> dict:
    """Write backtest results + signal outcomes to DB.

    Shared helper for strategy run.py files. Extracts non-null signal values
    from the featured DataFrame, then writes everything in one call.

    OHLCV is written as a safety net (ON CONFLICT DO NOTHING). When using
    get_ohlcv() for data fetching, OHLCV is already in DB from the fetch step.
    """
    symbol_df = df.xs(symbol, level="symbol")
    raw = symbol_df[signal_column].astype(float)
    # WHY: keep all non-NaN signal values (positive, negative, zero).
    # NaN means "no signal at this bar" and is excluded from signal_outcomes.
    signal_series = raw.dropna()
    signal_series = signal_series[signal_series != 0]

    counts = write_backtest_output(output, signal_series=signal_series, params=params)

    # WHY: safety net — if get_ohlcv() already wrote to DB, this is a no-op
    # (ON CONFLICT DO NOTHING). If DB was unavailable during fetch, this
    # ensures OHLCV gets persisted with the backtest results.
    ohlcv_df = symbol_df[["open", "high", "low", "close", "volume"]]
    ohlcv_df.index.name = "ts"
    counts["ohlcv"] = write_ohlcv(ohlcv_df, symbol, timeframe)
    return counts
