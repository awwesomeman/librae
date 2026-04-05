"""TimescaleDB reader — query helpers for dashboards and analysis."""
from __future__ import annotations

import pandas as pd

from db import TIMESCALE_DSN, get_conn


def get_latest_run_id(strategy: str | None = None, dsn: str = TIMESCALE_DSN) -> str | None:
    """Return the most recent run_id, optionally filtered by strategy."""
    sql = "SELECT run_id FROM backtest_runs"
    params: list = []
    if strategy:
        sql += " WHERE strategy = %s"
        params.append(strategy)
    sql += " ORDER BY run_ts DESC LIMIT 1"
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


def list_runs(limit: int = 20, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    """List recent backtest runs."""
    sql = """
        SELECT run_id, strategy, symbol, timeframe,
               mode, data_source, start_ts, end_ts, run_ts
        FROM backtest_runs
        ORDER BY run_ts DESC
        LIMIT %s
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[limit])
    return df


def load_equity_curve(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    sql = """
        SELECT ts AS _time, equity, benchmark_equity, drawdown,
               ret_1d, benchmark_ret_1d
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
               quantity, price, entry_price, position_quantity, notional,
               commission, slippage, tax,
               pnl, net_return, entry_ts, holding_bars, reason
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


def load_strategy_signals(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    """Load entry/exit signals from trade_events (open=entry, close=exit)."""
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


def load_ohlcv(
    run_id: str | None = None,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    data_source: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
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
        if start_ts:
            sql += " AND ts >= %s"
            params.append(start_ts)
        if end_ts:
            sql += " AND ts <= %s"
            params.append(end_ts)
        sql += " ORDER BY ts"
    elif run_id:
        sql = """
            WITH meta AS (
                SELECT symbol, timeframe, start_ts, end_ts
                FROM backtest_runs WHERE run_id = %s
            )
            SELECT ts AS _time, o.symbol, open, high, low, close, volume
            FROM ohlcv o, meta m
            WHERE o.symbol = m.symbol
              AND o.timeframe = m.timeframe
              AND (m.start_ts IS NULL OR ts >= m.start_ts)
              AND (m.end_ts IS NULL OR ts <= m.end_ts)
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
