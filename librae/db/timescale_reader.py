"""TimescaleDB reader — query helpers for dashboards and analysis.

Naming convention:
    get_*    — single scalar / small object lookup (id, dict, list of tuples)
    load_*   — bulk query returning a DataFrame for analysis/dashboards
    derive_* — computes a differently-shaped result from stored data; not a
               raw table read
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from librae.backtest.schema import RunMetadata, StrategyMetrics
from librae.config.symbols import validate_instrument_type
from librae.core.market_data import MarketDataSubscription
from librae.db import get_conn

if TYPE_CHECKING:
    from collections.abc import Mapping


def _parse_primary_subscriptions(value: object) -> tuple[MarketDataSubscription, ...]:
    """Parse persisted exact identities without filling legacy omissions."""
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        raise ValueError("primary_subscriptions must be a JSON array")
    subscriptions = tuple(MarketDataSubscription.from_dict(item) for item in decoded)
    if len(subscriptions) != len(set(subscriptions)):
        raise ValueError("primary_subscriptions must not contain duplicates")
    symbols = [item.symbol for item in subscriptions]
    if len(symbols) != len(set(symbols)):
        raise ValueError("primary_subscriptions must contain one identity per symbol")
    return subscriptions


def _query_timestamp(value: object, *, field_name: str) -> datetime:
    """Normalize one query frontier while rejecting ambiguous local time."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return timestamp.tz_convert("UTC").to_pydatetime()


def get_run_by_backtest_cache_key(
    backtest_cache_key: str,
    dsn: str | None = None,
) -> dict[str, Any] | None:
    """Find the canonical run for an explicit backtest cache identity."""
    sql = """SELECT run_id, params, execution_policy, risk_policy, session_mode
             FROM backtest_runs
             WHERE backtest_cache_key = %s
             ORDER BY run_at DESC LIMIT 1"""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, (backtest_cache_key,))
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return {
        "run_id": row[0],
        "params": json.loads(row[1]) if row[1] else None,
        "execution_policy": json.loads(row[2]) if row[2] else None,
        "risk_policy": json.loads(row[3]) if row[3] else None,
        "session_mode": row[4],
    }


def get_run_by_config_hash(
    config_hash: str,
    dsn: str | None = None,
) -> dict[str, Any] | None:
    """Find the most recent run with the given config_hash.

    Returns run configuration metadata, or None if not found.
    Existing old runs (config_hash=NULL) are not affected.
    """
    sql = """SELECT run_id, params, execution_policy, risk_policy, session_mode
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
        "session_mode": row[4],
    }


def get_run(run_id: str, dsn: str | None = None) -> RunMetadata | None:
    """Look up one run's identity by its primary key, or None if not found.

    Returns the run's RunMetadata (strategy_name/symbols/timeframe/data_source/
    started_at/ended_at/run_at/mode) — the identity fields a caller needs to
    label a report or chart. For the resolved config used to decide backtest
    cache reuse, see get_run_by_config_hash()/get_run_by_backtest_cache_key().
    """
    sql = """SELECT run_id, strategy_name, symbols, timeframe, data_source,
                     started_at, ended_at, run_at, mode, session_mode,
                     primary_subscriptions
             FROM backtest_runs
             WHERE run_id = %s"""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, (run_id,))
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return RunMetadata(
        run_id=row[0],
        strategy_name=row[1],
        symbols=json.loads(row[2]),
        timeframe=row[3],
        data_source=row[4],
        started_at=row[5],
        ended_at=row[6],
        run_at=row[7],
        mode=row[8],
        session_mode=row[9],
        primary_subscriptions=_parse_primary_subscriptions(row[10]),
    )


def get_latest_run_id(strategy_name: str | None = None, dsn: str | None = None) -> str | None:
    """Return the most recent run_id, optionally filtered by strategy."""
    sql = "SELECT run_id FROM backtest_runs"
    params: list = []
    if strategy_name:
        sql += " WHERE strategy_name = %s"
        params.append(strategy_name)
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
        SELECT run_id, strategy_name, symbols, timeframe,
               mode, data_source, session_mode, started_at, ended_at, run_at
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


def load_position_events(
    run_id: str,
    *,
    event_types: list[str] | None = None,
    account_id: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load position_events for a run, ordered by timestamp.

    Args:
        event_types: Optional filter, e.g. ["close", "reduce"] for closed trades only.
    """
    sql = """
        SELECT event_id, ts AS _time, account_id, currency,
               symbol, side, event_type,
               fill_quantity, price, entry_price, remaining_quantity, notional,
               commission, slippage, tax,
               entry_commission, entry_slippage, entry_tax,
               pnl, net_return, entry_at, periods_held, reason, group_id, time_in_force
        FROM position_events
        WHERE run_id = %s
    """
    params: list = [run_id]
    if account_id is not None:
        sql += " AND account_id = %s"
        params.append(account_id)
    if event_types:
        sql += " AND event_type = ANY(%s)"
        params.append(event_types)
    sql += " ORDER BY ts, event_id"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty:
        if "_time" in df.columns:
            df["_time"] = pd.to_datetime(df["_time"], utc=True)
        if "entry_at" in df.columns:
            df["entry_at"] = pd.to_datetime(df["entry_at"], utc=True)
    return df


def load_symbols(
    *,
    market: str | None = None,
    instrument_type: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load the instrument master, optionally narrowed to one market or type.

    Join it to a fact table on (symbol, data_source, instrument_type) to
    answer questions the bare `symbol` string cannot -- which contracts share
    a market, what a symbol's multiplier or trading calendar is.
    """
    sql = "SELECT * FROM symbols"
    conditions: list[str] = []
    params: list = []
    if market is not None:
        conditions.append("market = %s")
        params.append(market)
    if instrument_type is not None:
        validate_instrument_type(instrument_type)
        conditions.append("instrument_type = %s")
        params.append(instrument_type)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY market, symbol, data_source"
    with get_conn(dsn) as conn:
        return pd.read_sql(sql, conn, params=params or None)


def load_financing_cash_flows(
    run_id: str,
    *,
    account_id: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load applied financing payments (funding and short borrow) for a run."""
    sql = """
        SELECT ts AS _time, account_id, currency, symbol, kind, side,
               quantity, mark_price, multiplier, rate, cash_flow,
               group_id, entry_at
        FROM financing_cash_flows
        WHERE run_id = %s
    """
    params: list = [run_id]
    if account_id is not None:
        sql += " AND account_id = %s"
        params.append(account_id)
    sql += " ORDER BY account_id, ts, symbol, kind"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty:
        if "_time" in df.columns:
            df["_time"] = pd.to_datetime(df["_time"], utc=True)
        if "entry_at" in df.columns:
            df["entry_at"] = pd.to_datetime(df["entry_at"], utc=True)
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
               br.strategy_name, br.symbols, br.timeframe
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


def row_to_strategy_metrics(row: Mapping[str, Any]) -> StrategyMetrics:
    """Convert one ``load_performance()`` row into a ``StrategyMetrics`` record."""
    field_names = {f.name for f in fields(StrategyMetrics)}
    return StrategyMetrics(**{name: row[name] for name in field_names})


def derive_trade_signals(run_id: str, dsn: str | None = None) -> pd.DataFrame:
    """Derive a synthetic entry/exit signal series from position_events (open=entry,
    close/reduce=exit) — i.e. the strategy_name's actual executed fills, NOT a read of
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
        FROM position_events
        WHERE run_id = %s
        ORDER BY ts
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def get_ohlcv_coverage_ranges(
    subscription: MarketDataSubscription,
    dsn: str | None = None,
) -> list[tuple[datetime, datetime]]:
    """Return this key's cached (range_started_at, range_ended_at) pairs, sorted.

    May be several disjoint ranges (e.g. an old backfill plus a recent
    window with a gap between them) — see merge_ohlcv_coverage_ranges() for how
    they're kept merged/deduplicated on write.
    """
    if not isinstance(subscription, MarketDataSubscription):
        raise TypeError("subscription must be a MarketDataSubscription")
    sql = """
        SELECT range_started_at, range_ended_at FROM ohlcv_coverage_ranges
        WHERE symbol = %s AND timeframe = %s AND calendar_id = %s
              AND session_mode = %s AND data_source = %s AND instrument_type = %s
        ORDER BY range_started_at
    """
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            sql,
            (
                subscription.symbol,
                subscription.timeframe,
                subscription.calendar_id,
                subscription.session_mode,
                subscription.data_source,
                subscription.instrument_type,
            ),
        )
        rows = cur.fetchall()
        cur.close()
    return [(r[0], r[1]) for r in rows]


def get_external_factor_coverage_ranges(
    symbol: str,
    factor_name: str,
    timeframe: str,
    data_source: str,
    instrument_type: str = "spot",
    dsn: str | None = None,
) -> list[tuple[datetime, datetime]]:
    """Return this factor key's cached (range_started_at, range_ended_at) pairs,
    sorted. Same shape/semantics as get_ohlcv_coverage_ranges()."""
    sql = """
        SELECT range_started_at, range_ended_at FROM external_factor_coverage_ranges
        WHERE symbol = %s AND factor_name = %s AND timeframe = %s
              AND data_source = %s AND instrument_type = %s
        ORDER BY range_started_at
    """
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql, (symbol, factor_name, timeframe, data_source, instrument_type))
        rows = cur.fetchall()
        cur.close()
    return [(r[0], r[1]) for r in rows]


def load_external_factor(
    symbol: str,
    factor_name: str,
    timeframe: str,
    data_source: str,
    *,
    instrument_type: str = "spot",
    started_at: str | None = None,
    ended_at: str | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load cached factor values for (symbol, factor_name, timeframe,
    data_source, instrument_type).

    Returns DataFrame with columns [timestamp, value], tz-aware UTC, sorted
    ascending — same shape get_factor()'s fetchers must return.
    """
    sql = """
        SELECT ts AS timestamp, value FROM external_factors
        WHERE symbol = %s AND factor_name = %s AND timeframe = %s
              AND data_source = %s AND instrument_type = %s
    """
    params: list = [symbol, factor_name, timeframe, data_source, instrument_type]
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
    subscription: MarketDataSubscription | None = None,
    started_at: str | datetime | None = None,
    ended_at: str | datetime | None = None,
    as_of: str | datetime | None = None,
    dsn: str | None = None,
) -> pd.DataFrame:
    """Load OHLCV by one exact subscription or authoritative run metadata.

    ``as_of`` filters by ``available_at`` and is the causal frontier for DB
    warmup/as-of consumers. Legacy runs without complete subscriptions fail
    closed instead of falling back to partial run-level metadata.
    """
    if run_id is not None and subscription is not None:
        raise ValueError("pass either run_id or subscription, not both")
    frontier = _query_timestamp(as_of, field_name="as_of") if as_of is not None else None
    if subscription is not None:
        if not isinstance(subscription, MarketDataSubscription):
            raise TypeError("subscription must be a MarketDataSubscription")
        sql = """
            SELECT ts AS _time, symbol, timeframe, calendar_id, session_mode,
                   data_source, instrument_type, available_at,
                   open, high, low, close, volume
            FROM ohlcv
            WHERE symbol = %s AND timeframe = %s AND calendar_id = %s
              AND session_mode = %s AND data_source = %s AND instrument_type = %s
        """
        params: list[object] = list(subscription.to_dict().values())
        if started_at:
            sql += " AND ts >= %s"
            params.append(_query_timestamp(started_at, field_name="started_at"))
        if ended_at:
            sql += " AND ts <= %s"
            params.append(_query_timestamp(ended_at, field_name="ended_at"))
        if frontier is not None:
            sql += " AND available_at <= %s"
            params.append(frontier)
        sql += " ORDER BY ts"
    elif run_id:
        with get_conn(dsn) as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT primary_subscriptions, started_at, ended_at
                   FROM backtest_runs WHERE run_id = %s""",
                (run_id,),
            )
            metadata = cur.fetchone()
            cur.close()
        if metadata is None:
            return pd.DataFrame()
        subscriptions = _parse_primary_subscriptions(metadata[0])
        if not subscriptions:
            raise ValueError(
                f"run {run_id!r} has no exact primary_subscriptions; "
                "recreate or explicitly migrate the legacy run metadata"
            )
        placeholders = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(subscriptions))
        sql = """
            SELECT ts AS _time, o.symbol, o.timeframe, o.calendar_id, o.session_mode,
                   o.data_source, o.instrument_type, o.available_at,
                   open, high, low, close, volume
            FROM ohlcv o
            JOIN (VALUES {placeholders}) AS route(
                symbol, timeframe, calendar_id, session_mode, data_source, instrument_type
            )
              ON o.symbol = route.symbol
             AND o.timeframe = route.timeframe
             AND o.calendar_id = route.calendar_id
             AND o.session_mode = route.session_mode
             AND o.data_source = route.data_source
             AND o.instrument_type::text = route.instrument_type
        """
        sql = sql.format(placeholders=placeholders)
        params = [value for item in subscriptions for value in item.to_dict().values()]
        predicates: list[str] = []
        if metadata[1] is not None:
            predicates.append("o.ts >= %s")
            params.append(metadata[1])
        if metadata[2] is not None:
            predicates.append("o.ts <= %s")
            params.append(metadata[2])
        if frontier is not None:
            predicates.append("o.available_at <= %s")
            params.append(frontier)
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        sql += " ORDER BY ts, o.symbol"
    else:
        return pd.DataFrame()

    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty and "_time" in df.columns:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
        if "available_at" in df.columns:
            df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    return df
