"""TimescaleDB reader — query helpers for dashboards and analysis.

Naming convention:
    get_*    — single scalar / small object lookup (id, dict, list of tuples)
    load_*   — bulk query returning a DataFrame for analysis/dashboards
    derive_* — computes a differently-shaped result from stored data; not a
               raw table read
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd

from db import TIMESCALE_DSN, get_conn


def get_run_by_config_hash(
    config_hash: str,
    dsn: str = TIMESCALE_DSN,
) -> dict[str, Any] | None:
    """Find the most recent run with the given config_hash.

    Returns dict with run_id, params, perf_params, or None if not found.
    Existing old runs (config_hash=NULL) are not affected.
    """
    sql = """SELECT run_id, params, perf_params
             FROM backtest_runs
             WHERE config_hash = %s
             ORDER BY run_at DESC LIMIT 1"""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, (config_hash,))
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return {
        "run_id": row[0],
        "params": json.loads(row[1]) if row[1] else None,
        "perf_params": json.loads(row[2]) if row[2] else None,
    }


def get_latest_run_id(strategy: str | None = None, dsn: str = TIMESCALE_DSN) -> str | None:
    """Return the most recent run_id, optionally filtered by strategy."""
    sql = "SELECT run_id FROM backtest_runs"
    params: list = []
    if strategy:
        sql += " WHERE strategy = %s"
        params.append(strategy)
    sql += " ORDER BY run_at DESC LIMIT 1"
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


def load_runs(limit: int = 20, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    """List recent backtest runs."""
    sql = """
        SELECT run_id, strategy, symbol, timeframe,
               mode, data_source, started_at, ended_at, run_at
        FROM backtest_runs
        ORDER BY run_at DESC
        LIMIT %s
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[limit])
    return df


def load_equity_curve(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    sql = """
        SELECT ts AS _time, equity, benchmark_equity, drawdown,
               period_return, benchmark_period_return
        FROM equity_curve
        WHERE run_id = %s
        ORDER BY ts
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_trade_events(
    run_id: str,
    *,
    event_types: list[str] | None = None,
    dsn: str = TIMESCALE_DSN,
) -> pd.DataFrame:
    """Load trade_events for a run, ordered by timestamp.

    Args:
        event_types: Optional filter, e.g. ["close", "reduce"] for closed trades only.
    """
    sql = """
        SELECT event_id, ts AS _time, symbol, side, event_type,
               fill_quantity, price, entry_price, remaining_quantity, notional,
               commission, slippage, tax,
               pnl, net_return, entry_at, periods_held, reason
        FROM trade_events
        WHERE run_id = %s
    """
    params: list = [run_id]
    if event_types:
        sql += " AND event_type = ANY(%s)"
        params.append(event_types)
    sql += " ORDER BY ts"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_performance(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    """Load strategy_performance joined with backtest_runs.

    Returns a column-based DataFrame with one row containing all metrics.
    """
    sql = """
        SELECT sp.run_id, sp.total_return, sp.annual_return, sp.sharpe, sp.sortino,
               sp.calmar, sp.max_drawdown, sp.win_rate, sp.profit_factor, sp.trades,
               sp.avg_trade_return, sp.exposure_ratio, sp.benchmark_return,
               sp.total_commission, sp.total_slippage, sp.total_tax,
               br.strategy, br.symbol, br.timeframe
        FROM strategy_performance sp
        JOIN backtest_runs br ON sp.run_id = br.run_id
        WHERE sp.run_id = %s
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    return df


def derive_trade_signals(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    """Derive a synthetic entry/exit signal series from trade_events (open=entry,
    close/reduce=exit) — i.e. the strategy's actual executed fills, NOT a read of
    the separate signal_events table (which stores raw pre-execution signals for
    quality monitoring)."""
    sql = """
        SELECT ts AS _time, symbol,
               CASE WHEN event_type IN ('open', 'add') THEN 'entry' ELSE 'exit' END AS signal_type,
               price,
               CASE WHEN side='long' AND event_type IN ('open','add') THEN 1.0
                    WHEN side='short' AND event_type IN ('open','add') THEN -1.0
                    WHEN side='long' AND event_type IN ('reduce','close') THEN -1.0
                    ELSE 1.0
               END AS signal_strength,
               run_id
        FROM trade_events
        WHERE run_id = %s
        ORDER BY ts
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def get_ohlcv_coverage_ranges(
    symbol: str,
    timeframe: str,
    data_source: str,
    dsn: str = TIMESCALE_DSN,
) -> list[tuple[datetime, datetime]]:
    """Return this key's cached (range_started_at, range_ended_at) pairs, sorted.

    May be several disjoint ranges (e.g. an old backfill plus a recent
    window with a gap between them) — see merge_ohlcv_coverage_ranges() for how
    they're kept merged/deduplicated on write.
    """
    sql = """
        SELECT range_started_at, range_ended_at FROM ohlcv_coverage_ranges
        WHERE symbol = %s AND timeframe = %s AND data_source = %s
        ORDER BY range_started_at
    """
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, (symbol, timeframe, data_source))
        rows = cur.fetchall()
        cur.close()
    return [(r[0], r[1]) for r in rows]


def load_ohlcv(
    run_id: str | None = None,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    data_source: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    dsn: str = TIMESCALE_DSN,
) -> pd.DataFrame:
    """Load OHLCV data by symbol+timeframe+range, or by run_id.

    When *run_id* is given (without symbol/timeframe), the run's metadata
    is used to derive symbol, timeframe, and date range.
    """
    if symbol and timeframe:
        sql = """
            SELECT ts AS _time, symbol, open, high, low, close, volume
            FROM ohlcv
            WHERE symbol = %s AND timeframe = %s
        """
        params: list = [symbol, timeframe]
        if data_source:
            sql += " AND data_source = %s"
            params.append(data_source)
        if started_at:
            sql += " AND ts >= %s"
            params.append(started_at)
        if ended_at:
            sql += " AND ts <= %s"
            params.append(ended_at)
        sql += " ORDER BY ts"
    elif run_id:
        sql = """
            WITH meta AS (
                SELECT symbol, timeframe, started_at, ended_at
                FROM backtest_runs WHERE run_id = %s
            )
            SELECT ts AS _time, o.symbol, open, high, low, close, volume
            FROM ohlcv o, meta m
            WHERE o.symbol = m.symbol
              AND o.timeframe = m.timeframe
              AND (m.started_at IS NULL OR ts >= m.started_at)
              AND (m.ended_at IS NULL OR ts <= m.ended_at)
            ORDER BY ts
        """
        params = [run_id]
    else:
        return pd.DataFrame()

    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df
