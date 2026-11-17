"""TimescaleDB reader — query helpers for dashboards and analysis.

Naming convention:
    get_*    — single scalar / small object lookup (id, dict, list of tuples)
    load_*   — bulk query returning a DataFrame for analysis/dashboards
    derive_* — computes a differently-shaped result from stored data; not a
               raw table read
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import fields
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from librae.backtest.schema import (
    AccountPerformance,
    BacktestOutput,
    EquityCurvePoint,
    FinancingCashFlowRecord,
    PositionEventRecord,
    RunMetadata,
    StrategyMetrics,
)
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
        # psycopg2 decodes jsonb columns itself.
        "params": row[1],
        "execution_policy": row[2],
        "risk_policy": row[3],
        "session_mode": row[4],
    }


def get_run(run_id: str, dsn: str | None = None) -> RunMetadata | None:
    """Look up one run's identity by its primary key, or None if not found.

    Returns the run's RunMetadata (strategy_name/symbols/timeframe/data_source/
    started_at/ended_at/run_at/mode) — the identity fields a caller needs to
    label a report or chart. For the resolved config used to decide backtest
    cache reuse, see get_run_by_backtest_cache_key().
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
        symbols=tuple(json.loads(row[2]) if isinstance(row[2], str) else row[2]),
        timeframe=row[3],
        data_source=row[4],
        started_at=row[5],
        ended_at=row[6],
        run_at=row[7],
        mode=row[8],
        session_mode=row[9],
        primary_subscriptions=_parse_primary_subscriptions(row[10]),
    )


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
               realized_pnl, net_return, entry_at, periods_held, reason, group_id, time_in_force
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
    # Event ids of one run differ only in their numeric suffix, so ordering by
    # length first puts "-e10000" after "-e9999", in the order they happened.
    sql += " ORDER BY ts, length(event_id), event_id"
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty:
        if "_time" in df.columns:
            df["_time"] = pd.to_datetime(df["_time"], utc=True)
        if "entry_at" in df.columns:
            df["entry_at"] = pd.to_datetime(df["entry_at"], utc=True)
    return df


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


def load_signal_events(run_id: str, dsn: str | None = None) -> pd.DataFrame:
    """Load a run's signals, ordered by timestamp, with the inputs behind each.

    ``inputs`` holds the dict the strategy gave in its ``signal_inputs``
    column on that bar, or None when it gave none. ``price`` is the decision
    price a sim or live engine saw; a backtest stores none.
    """
    sql = """
        SELECT ts AS _time, symbol, signal_type, signal_value, price, inputs
        FROM signal_events
        WHERE run_id = %s
        ORDER BY ts, symbol, signal_type
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty:
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


def load_run_bars(run_id: str, dsn: str | None = None) -> pd.DataFrame:
    """Load the bars one run stored, the latest version of each.

    A sim or live run stores the bars behind its decisions; a backtest stores its inputs
    only when saved with ``store_bars=True``, so an empty frame is the normal
    answer for one that was not. Earlier versions of a bar stay in
    ``run_bars`` for audit; this returns the one the run received last, which
    is the one it ended up using (bars a backtest stored have no receipt time
    and fall back to the latest ``available_at``).
    """
    sql = """
        SELECT DISTINCT ON (symbol, ts)
               ts AS _time, symbol, timeframe, calendar_id, session_mode,
               data_source, instrument_type, available_at, observed_at,
               open, high, low, close, volume
        FROM run_bars
        WHERE run_id = %s
        ORDER BY symbol, ts, observed_at DESC NULLS LAST, available_at DESC
    """
    with get_conn(dsn) as conn:
        df = pd.read_sql(sql, conn, params=[run_id])
    if not df.empty:
        for column in ("_time", "available_at", "observed_at"):
            df[column] = pd.to_datetime(df[column], utc=True)
    return df


def load_backtest_output(run_id: str) -> BacktestOutput:
    """Rebuild one stored run as the ``BacktestOutput`` it was saved from.

    Metrics are the stored ``strategy_performance`` row, not recomputed, so a
    report agrees with every other reader of that row. A sim or live run still
    going ends at its last equity point. Position and allocation snapshots and
    runtime events are not part of the result.

    Raises ``ValueError`` for an unknown run, one with no performance row yet
    (a sim or live run gets one at its first close or settlement), or one
    with more than one account.
    """
    run = get_run(run_id)
    if run is None:
        raise ValueError(f"no stored run {run_id!r}")
    performance = load_performance(run_id)
    if performance.empty:
        raise ValueError(f"run {run_id!r} has no strategy_performance row")
    if len(performance) > 1:
        raise ValueError(f"run {run_id!r} has {len(performance)} accounts; expected one")
    row = {key: _plain(value) for key, value in performance.iloc[0].items()}
    account_id = row["account_id"]
    equity = load_equity_curve(run_id, account_id=account_id)
    if equity.empty:
        raise ValueError(f"run {run_id!r} has no equity curve")
    if run.ended_at is None:
        run = dataclasses.replace(run, ended_at=equity["_time"].iloc[-1].to_pydatetime())
    return BacktestOutput(
        run_metadata=run,
        account=AccountPerformance(
            account_id=account_id,
            currency=row["currency"],
            initial_cash=float(row["initial_cash"]),
            final_equity=float(row["final_equity"]),
            net_pnl=float(row["net_pnl"]),
            equity_curve=_records(equity, EquityCurvePoint),
            metrics=row_to_strategy_metrics(row),
        ),
        position_events=_records(
            load_position_events(run_id, account_id=account_id), PositionEventRecord
        ),
        position_snapshots=(),
        allocation_snapshots=(),
        financing_cash_flows=_records(
            load_financing_cash_flows(run_id, account_id=account_id), FinancingCashFlowRecord
        ),
    )


def _plain(value: Any) -> Any:
    """A DataFrame cell as the builtin value the record types hold; NULL is None."""
    if value is None or (not isinstance(value, (list, tuple, dict)) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item") and not isinstance(value, datetime):
        return value.item()
    return value


def _records(frame: pd.DataFrame, record_type: type) -> tuple:
    """Rows of a ``load_*`` frame as ``record_type``; a NULL takes the field's default."""
    record_fields = dataclasses.fields(record_type)
    names = {field.name for field in record_fields}
    with_default = {
        field.name
        for field in record_fields
        if field.default is not dataclasses.MISSING
        or field.default_factory is not dataclasses.MISSING
    }
    records = []
    for row in frame.rename(columns={"_time": "ts"}).to_dict("records"):
        values = {key: _plain(value) for key, value in row.items() if key in names}
        records.append(
            record_type(
                **{
                    key: value
                    for key, value in values.items()
                    if value is not None or key not in with_default
                }
            )
        )
    return tuple(records)
