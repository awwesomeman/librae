"""TimescaleDB reader for Streamlit dashboard."""
from __future__ import annotations

from contextlib import contextmanager

import pandas as pd
import psycopg2

TIMESCALE_DSN = "postgresql://quant:quant_secret@localhost:5432/quant"


@contextmanager
def _conn(dsn: str = TIMESCALE_DSN):
    conn = psycopg2.connect(dsn)
    try:
        yield conn
    finally:
        conn.close()


def get_latest_run_id(strategy: str | None = None, dsn: str = TIMESCALE_DSN) -> str | None:
    """Return the most recent run_id, optionally filtered by strategy."""
    sql = "SELECT run_id FROM backtest_runs"
    params: list = []
    if strategy:
        sql += " WHERE strategy = %s"
        params.append(strategy)
    sql += " ORDER BY run_ts DESC LIMIT 1"
    with _conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


def list_runs(limit: int = 20, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    """List recent backtest runs."""
    sql = """
        SELECT run_id, strategy, symbol, timeframe, sample,
               start_ts, end_ts, run_ts
        FROM backtest_runs
        ORDER BY run_ts DESC
        LIMIT %s
    """
    with _conn(dsn) as conn:
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
    with _conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_trade_blotter(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    sql = """
        SELECT trade_id, entry_ts, exit_ts AS _time, side,
               entry_price, exit_price, quantity,
               gross_pnl, net_pnl, commission, holding_bars, symbol
        FROM trade_blotter
        WHERE run_id = %s
        ORDER BY entry_ts DESC
    """
    with _conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty:
        if "_time" in df.columns:
            df["_time"] = pd.to_datetime(df["_time"], utc=True)
        if "entry_ts" in df.columns:
            df["entry_time"] = pd.to_datetime(df["entry_ts"], utc=True).dt.strftime("%Y-%m-%d %H:%M")
    return df


def load_performance(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    """Load strategy_performance joined with backtest_runs.

    Returns a DataFrame with one row per metric in _field/_value format
    (matching the InfluxDB pivot layout expected by the Streamlit app).
    """
    sql = """
        SELECT sp.*, br.strategy, br.symbol, br.timeframe, br.sample
        FROM strategy_performance sp
        JOIN backtest_runs br ON sp.run_id = br.run_id
        WHERE sp.run_id = %s
    """
    with _conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if df.empty:
        return df

    # Pivot to _field/_value format matching InfluxDB convention
    metric_cols = [
        "total_return", "annual_return", "sharpe", "sortino",
        "max_drawdown", "win_rate", "profit_factor", "trades",
        "avg_trade_return", "exposure_ratio", "bh_total_return",
    ]
    rows = []
    row = df.iloc[0]
    for col in metric_cols:
        if col in row.index and row[col] is not None:
            rows.append({
                "_field": col,
                "_value": float(row[col]) if row[col] is not None else 0.0,
                "strategy": row.get("strategy"),
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "sample": row.get("sample"),
            })
    return pd.DataFrame(rows)


def load_strategy_signals(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    sql = """
        SELECT ts AS _time, strategy, symbol, timeframe,
               signal_type AS side, source, price,
               signal_strength, confidence, quantity, run_id
        FROM strategy_signals
        WHERE run_id = %s
        ORDER BY ts
    """
    with _conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_ohlcv(run_id: str, dsn: str = TIMESCALE_DSN) -> pd.DataFrame:
    sql = """
        SELECT ts AS _time, symbol, open, high, low, close, volume
        FROM ohlcv
        WHERE run_id = %s
        ORDER BY ts
    """
    with _conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df
