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

from librae.db import get_conn


def get_run_by_config_hash(
    config_hash: str,
    dsn: str | None = None,
) -> dict[str, Any] | None:
    """Find the most recent run with the given config_hash.

    Returns run configuration metadata, or None if not found.
    Existing old runs (config_hash=NULL) are not affected.
    """
    sql = """SELECT run_id, params, execution_policy, risk_policy
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
        "execution_policy": json.loads(row[2]) if row[2] else None,
        "risk_policy": json.loads(row[3]) if row[3] else None,
    }


def get_latest_run_id(strategy: str | None = None, dsn: str | None = None) -> str | None:
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


def load_runs(limit: int = 20, dsn: str | None = None) -> pd.DataFrame:
    """List recent backtest runs."""
    sql = """
        SELECT run_id, strategy, symbols, timeframe,
               mode, data_source, started_at, ended_at, run_at
        FROM backtest_runs
        ORDER BY run_at DESC
        LIMIT %s
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[limit])
    return df


def load_equity_curve(
    run_id: str,
    *,
    account_id: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    sql = """
        SELECT ts AS _time, account_id, currency,
               equity, drawdown, period_return, gross_exposure,
               net_exposure, concentration, turnover, exposed
        FROM equity_curve
        WHERE run_id = %s
    """
    params: list = [run_id]
    if account_id is not None:
        sql += " AND account_id = %s"
        params.append(account_id)
    sql += " ORDER BY account_id, ts"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_trade_events(
    run_id: str,
    *,
    event_types: list[str] | None = None,
    account_id: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load trade_events for a run, ordered by timestamp.

    Args:
        event_types: Optional filter, e.g. ["close", "reduce"] for closed trades only.
    """
    sql = """
        SELECT event_id, ts AS _time, account_id, currency,
               symbol, side, event_type,
               fill_quantity, price, entry_price, remaining_quantity, notional,
               commission, slippage, tax,
               entry_commission, entry_slippage, entry_tax,
               pnl, net_return, entry_at, periods_held, reason
        FROM trade_events
        WHERE run_id = %s
    """
    params: list = [run_id]
    if account_id is not None:
        sql += " AND account_id = %s"
        params.append(account_id)
    if event_types:
        sql += " AND event_type = ANY(%s)"
        params.append(event_types)
    sql += " ORDER BY ts"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_funding_cash_flows(
    run_id: str,
    *,
    account_id: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load applied perpetual-funding payments for a run."""
    sql = """
        SELECT ts AS _time, account_id, currency, symbol, side,
               quantity, mark_price, multiplier, rate, cash_flow
        FROM funding_cash_flows
        WHERE run_id = %s
    """
    params: list = [run_id]
    if account_id is not None:
        sql += " AND account_id = %s"
        params.append(account_id)
    sql += " ORDER BY account_id, ts, symbol"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def load_performance(
    run_id: str,
    *,
    account_id: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load strategy_performance joined with backtest_runs.

    Returns one currency-labeled row per account, or one selected account.
    """
    sql = """
        SELECT sp.run_id, sp.account_id, sp.currency, sp.initial_cash,
               sp.final_equity, sp.net_pnl,
               sp.total_return, sp.mean_period_return, sp.period_volatility,
               sp.period_downside_deviation, sp.period_sharpe, sp.period_sortino,
               sp.positive_period_rate, sp.max_drawdown, sp.win_rate,
               sp.profit_factor, sp.payoff_ratio, sp.trades,
               sp.avg_trade_return, sp.exposure_ratio, sp.total_turnover,
               sp.average_gross_exposure, sp.max_gross_exposure,
               sp.max_abs_net_exposure, sp.max_concentration,
               sp.total_commission, sp.total_slippage, sp.total_tax,
               br.strategy, br.symbols, br.timeframe
        FROM strategy_performance sp
        JOIN backtest_runs br ON sp.run_id = br.run_id
        WHERE sp.run_id = %s
    """
    params = [run_id]
    if account_id is not None:
        sql += " AND sp.account_id = %s"
        params.append(account_id)
    sql += " ORDER BY sp.account_id"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    return df


def derive_trade_signals(run_id: str, dsn: str | None = None) -> pd.DataFrame:
    """Derive a synthetic entry/exit signal series from trade_events (open=entry,
    close/reduce=exit) — i.e. the strategy's actual executed fills, NOT a read of
    the separate signal_events table (which stores raw pre-execution signals for
    quality monitoring)."""
    sql = """
        SELECT ts AS _time, account_id, currency, symbol,
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
    instrument_type: str = "spot",
    dsn: str | None = None,
) -> list[tuple[datetime, datetime]]:
    """Return this key's cached (range_started_at, range_ended_at) pairs, sorted.

    May be several disjoint ranges (e.g. an old backfill plus a recent
    window with a gap between them) — see merge_ohlcv_coverage_ranges() for how
    they're kept merged/deduplicated on write.
    """
    sql = """
        SELECT range_started_at, range_ended_at FROM ohlcv_coverage_ranges
        WHERE symbol = %s AND timeframe = %s AND data_source = %s AND instrument_type = %s
        ORDER BY range_started_at
    """
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, (symbol, timeframe, data_source, instrument_type))
        rows = cur.fetchall()
        cur.close()
    return [(r[0], r[1]) for r in rows]


def get_external_factor_coverage_ranges(
    symbol: str,
    factor_name: str,
    source: str,
    instrument_type: str = "spot",
    dsn: str | None = None,
) -> list[tuple[datetime, datetime]]:
    """Return this factor key's cached (range_started_at, range_ended_at) pairs,
    sorted. Same shape/semantics as get_ohlcv_coverage_ranges()."""
    sql = """
        SELECT range_started_at, range_ended_at FROM external_factor_coverage_ranges
        WHERE symbol = %s AND factor_name = %s AND source = %s AND instrument_type = %s
        ORDER BY range_started_at
    """
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, (symbol, factor_name, source, instrument_type))
        rows = cur.fetchall()
        cur.close()
    return [(r[0], r[1]) for r in rows]


def load_external_factor(
    symbol: str,
    factor_name: str,
    source: str,
    *,
    instrument_type: str = "spot",
    started_at: str | None = None,
    ended_at: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load cached factor values for (symbol, factor_name, source, instrument_type).

    Returns DataFrame with columns [timestamp, value], tz-aware UTC, sorted
    ascending — same shape get_factor()'s fetchers must return.
    """
    sql = """
        SELECT ts AS timestamp, value FROM external_factors
        WHERE symbol = %s AND factor_name = %s AND source = %s AND instrument_type = %s
    """
    params: list = [symbol, factor_name, source, instrument_type]
    if started_at:
        sql += " AND ts >= %s"
        params.append(started_at)
    if ended_at:
        sql += " AND ts <= %s"
        params.append(ended_at)
    sql += " ORDER BY ts"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def load_ohlcv(
    run_id: str | None = None,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    data_source: str | None = None,
    instrument_type: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load OHLCV data by symbol+timeframe+range, or by run_id.

    When *run_id* is given (without symbol/timeframe), the run's metadata
    is used to derive symbol, timeframe, and date range.

    instrument_type is an optional filter (None = don't filter, e.g. for
    dashboards that haven't been updated to pass it) — internal callers
    that write/read the same key (the OHLCV data-access layer) should always
    pass it explicitly to avoid silently mixing rows of different contract
    types that happen to share a symbol/data_source.
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
        if instrument_type:
            sql += " AND instrument_type = %s"
            params.append(instrument_type)
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
                SELECT symbols, timeframe, started_at, ended_at
                FROM backtest_runs WHERE run_id = %s
            )
            SELECT ts AS _time, o.symbol, open, high, low, close, volume
            FROM ohlcv o, meta m
            WHERE o.symbol IN (SELECT jsonb_array_elements_text(m.symbols))
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
