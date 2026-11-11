"""TimescaleDB writer — low-level writes and high-level save helpers.

Naming convention:
    write_*   — single-table INSERT/UPSERT (may include type/tz normalization),
                full-row write
    update_*  — single-table partial UPDATE of specific columns on an
                existing row
    save_*    — multi-table transactional orchestrator; may extract/derive
                data from broader inputs
    refresh_* — recompute derived/aggregate data from other tables and
                upsert the result
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg2
import psycopg2.extras

if TYPE_CHECKING:
    from psycopg2.extensions import cursor as PgCursor

    from librae.core.run_config import RunConfig

import pandas as pd

from librae.backtest.cache import build_backtest_cache_key, normalize_backtest_revision
from librae.backtest.schema import BacktestOutput
from librae.core.market_data import (
    AVAILABLE_AT_COLUMN,
    OBSERVED_AT_COLUMN,
    OHLCV_COLUMNS,
    MarketDataSubscription,
    normalize_bar_times,
    subscription_from_instrument,
    validate_bar_starts,
    validate_ohlcv_values,
)
from librae.core.utils import to_canonical
from librae.db import _bounded_statements, _storable_text, get_conn
from librae.db.schema import require_current_schema
from librae.live.execution_identity import ExecutionIdentity

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
        return ts.astimezone(UTC)
    return ts


_SIGNAL_INSERT_SQL = """INSERT INTO signal_events
           (ts, run_id, strategy_name, symbol, mode, timeframe,
            signal_value, price, signal_type)
           VALUES %s
           ON CONFLICT (ts, run_id, strategy_name, symbol, mode, timeframe, signal_type)
           DO NOTHING"""


def _normalize_data_source_by_symbol(
    symbols: tuple[str, ...] | list[str],
    data_source: str | None,
    data_source_by_symbol: Mapping[str, str] | None,
) -> dict[str, str]:
    """Return the exact per-symbol sources that identify one run's raw bars."""
    symbol_list = list(symbols)
    if data_source_by_symbol is None:
        if not data_source:
            return {}
        data_source_by_symbol = dict.fromkeys(symbol_list, data_source)

    expected = set(symbol_list)
    observed = set(data_source_by_symbol)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise ValueError(
            "data_source_by_symbol must cover exactly the run symbols; "
            f"missing={missing}, extra={extra}"
        )
    invalid = sorted(
        symbol
        for symbol, source in data_source_by_symbol.items()
        if not isinstance(source, str) or not source
    )
    if invalid:
        raise ValueError(f"data_source_by_symbol values must be non-empty strings: {invalid}")
    return {symbol: data_source_by_symbol[symbol] for symbol in symbol_list}


def _normalize_primary_subscriptions(
    symbols: Sequence[str],
    timeframe: str,
    session_mode: str,
    primary_subscriptions: Sequence[MarketDataSubscription],
) -> tuple[MarketDataSubscription, ...]:
    """Validate the authoritative run-to-raw-data identity mapping."""
    subscriptions = tuple(primary_subscriptions)
    if not subscriptions:
        raise ValueError(
            "primary_subscriptions is required; legacy run metadata cannot identify raw bars"
        )
    if any(not isinstance(item, MarketDataSubscription) for item in subscriptions):
        raise TypeError("primary_subscriptions must contain MarketDataSubscription values")
    expected_symbols = tuple(symbols)
    observed_symbols = tuple(item.symbol for item in subscriptions)
    if observed_symbols != expected_symbols:
        raise ValueError(
            "primary_subscriptions must follow and exactly cover run symbols; "
            f"expected={expected_symbols}, observed={observed_symbols}"
        )
    canonical_timeframe = to_canonical(timeframe)
    if any(item.timeframe != canonical_timeframe for item in subscriptions):
        raise ValueError("primary subscription timeframe must match run timeframe")
    if any(item.session_mode != session_mode for item in subscriptions):
        raise ValueError("primary subscription session_mode must match run session_mode")
    return subscriptions


def _extract_exit_signals(df: pd.DataFrame, symbol: str) -> pd.Series:
    """Extract exit_signal series if column exists, else empty Series."""
    if "exit_signal" not in df.columns:
        return pd.Series(dtype=float)
    _, series = _extract_signals(df, symbol, "exit_signal")
    return series


def _extract_signals(
    df: pd.DataFrame,
    symbol: str,
    signal_column: str = "entry_signal",
) -> tuple[pd.DataFrame, pd.Series]:
    """Extract symbol slice and non-zero signal values from a DataFrame.

    Returns (symbol_df, signal_series). Handles both MultiIndex and
    single-level DatetimeIndex inputs.
    """
    symbol_df = df.xs(symbol, level="symbol") if isinstance(df.index, pd.MultiIndex) else df
    raw = symbol_df[signal_column].astype(float)
    signal_series = raw.dropna()
    signal_series = signal_series[signal_series != 0]
    return symbol_df, signal_series


def write_run_metadata(
    run_id: str,
    strategy_name: str,
    symbols: tuple[str, ...] | list[str],
    timeframe: str,
    mode: str,
    *,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    run_at: datetime | None = None,
    data_source: str | None = None,
    data_source_by_symbol: Mapping[str, str] | None = None,
    session_mode: str = "extended",
    primary_subscriptions: Sequence[MarketDataSubscription],
    poll_seconds: int | None = None,
    params: dict | None = None,
    execution_policy: dict | None = None,
    risk_policy: dict | None = None,
    config_hash: str | None = None,
    backtest_revision: str | None = None,
    backtest_cache_key: str | None = None,
    execution_identity: dict[str, str] | None = None,
    cur: PgCursor | None = None,
    dsn: str | None = None,
) -> None:
    """Write a single run record to backtest_runs (upsert).

    The fields in the upsert's WHERE clause, mode included, are a run's
    fixed identity: an upsert that would change them is refused, so a
    backtest save cannot take over a sim or live run and clear the rows its
    engine recorded.

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction).  Otherwise opens its own connection and commits.
    """
    if mode == "live" and execution_identity is None:
        raise ValueError("live run metadata requires an execution_identity")
    if mode != "live" and execution_identity is not None:
        raise ValueError("execution_identity is only valid for live run metadata")
    if execution_identity is not None:
        execution_identity = ExecutionIdentity.from_dict(execution_identity).to_dict()
    timeframe = to_canonical(timeframe)
    if session_mode not in ("regular", "extended"):
        raise ValueError(f"invalid market-data session mode: {session_mode!r}")
    subscriptions = _normalize_primary_subscriptions(
        symbols,
        timeframe,
        session_mode,
        primary_subscriptions,
    )
    resolved_data_sources = {item.symbol: item.data_source for item in subscriptions}
    if data_source_by_symbol is not None:
        supplied_data_sources = _normalize_data_source_by_symbol(
            symbols,
            data_source,
            data_source_by_symbol,
        )
        if supplied_data_sources != resolved_data_sources:
            raise ValueError("data_source_by_symbol must match authoritative primary_subscriptions")
    sql = """INSERT INTO backtest_runs
               (run_id, strategy_name, symbols, timeframe, data_source,
                data_source_by_symbol, primary_subscriptions, session_mode,
                started_at, ended_at, run_at, mode, poll_seconds,
                params, execution_policy, risk_policy, config_hash,
                backtest_revision, backtest_cache_key, execution_identity)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                 strategy_name=EXCLUDED.strategy_name,
                 run_at=EXCLUDED.run_at,
                 poll_seconds=EXCLUDED.poll_seconds,
                 params=EXCLUDED.params,
                 execution_policy=EXCLUDED.execution_policy,
                 risk_policy=EXCLUDED.risk_policy,
                 config_hash=EXCLUDED.config_hash,
                 backtest_revision=EXCLUDED.backtest_revision,
                 backtest_cache_key=EXCLUDED.backtest_cache_key,
                 execution_identity=EXCLUDED.execution_identity
               WHERE backtest_runs.mode IS NOT DISTINCT FROM EXCLUDED.mode
                 AND backtest_runs.symbols = EXCLUDED.symbols
                 AND backtest_runs.timeframe = EXCLUDED.timeframe
                 AND backtest_runs.data_source IS NOT DISTINCT FROM EXCLUDED.data_source
                 AND backtest_runs.data_source_by_symbol = EXCLUDED.data_source_by_symbol
                 AND backtest_runs.primary_subscriptions = EXCLUDED.primary_subscriptions
                 AND backtest_runs.session_mode = EXCLUDED.session_mode
                 AND backtest_runs.execution_identity IS NOT DISTINCT FROM EXCLUDED.execution_identity"""
    params_val = json.dumps(params) if params is not None else None
    execution_policy_val = json.dumps(execution_policy) if execution_policy is not None else None
    risk_policy_val = json.dumps(risk_policy) if risk_policy is not None else None
    values = (
        run_id,
        strategy_name,
        json.dumps(symbols),
        timeframe,
        data_source,
        json.dumps(resolved_data_sources),
        json.dumps([item.to_dict() for item in subscriptions]),
        session_mode,
        _to_dt(started_at),
        _to_dt(ended_at),
        _to_dt(run_at) or datetime.now(tz=UTC),
        mode,
        poll_seconds,
        params_val,
        execution_policy_val,
        risk_policy_val,
        config_hash,
        backtest_revision,
        backtest_cache_key,
        json.dumps(execution_identity) if execution_identity is not None else None,
    )
    if cur is not None:
        require_current_schema(cur)
        cur.execute(sql, values)
        if cur.rowcount == 0:
            raise ValueError(f"run_id={run_id!r} already has a different immutable identity")
    else:
        with get_conn(dsn) as conn:
            c = conn.cursor()
            require_current_schema(c)
            c.execute(sql, values)
            if c.rowcount == 0:
                raise ValueError(f"run_id={run_id!r} already has a different immutable identity")
            c.close()


def update_heartbeat(run_id: str, dsn: str | None = None) -> None:
    """Update last_heartbeat_at timestamp for a running sim/live process."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE backtest_runs SET last_heartbeat_at = NOW() WHERE run_id = %s",
            (run_id,),
        )
        cur.close()


def _claim_backtest_cache_key(
    cur: PgCursor,
    backtest_cache_key: str | None,
    run_id: str,
    *,
    replace_existing: bool,
    store_bars: bool,
) -> bool:
    """Serialize one cache identity and decide whether this run may persist.

    A duplicate is skipped whole. One that asks to store bars is refused if
    the run it duplicates keeps none, since skipping would leave the caller
    without the bars it asked for.
    """
    if backtest_cache_key is None:
        return True

    # WHY: the CLI's pre-run dedup check cannot prevent two workers from
    # observing a miss concurrently. A transaction-scoped lock closes that
    # race without holding a session lock after commit or rollback.
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (backtest_cache_key,),
    )
    cur.execute(
        "SELECT run_id FROM backtest_runs WHERE backtest_cache_key = %s",
        (backtest_cache_key,),
    )
    existing = cur.fetchone()
    if existing is None or existing[0] == run_id:
        return True
    if replace_existing:
        # These two have no foreign key to cascade through.
        cur.execute("DELETE FROM signal_events WHERE run_id = %s", (existing[0],))
        cur.execute("DELETE FROM position_events WHERE run_id = %s", (existing[0],))
        cur.execute("DELETE FROM backtest_runs WHERE run_id = %s", (existing[0],))
        return True
    if store_bars:
        cur.execute("SELECT EXISTS (SELECT 1 FROM run_bars WHERE run_id = %s)", (existing[0],))
        if not cur.fetchone()[0]:
            raise ValueError(
                f"run_id={existing[0]!r} already holds this backtest_cache_key and keeps "
                "no bars; replace_existing=True (CLI --force) replaces that run with this one"
            )

    logger.info(
        "Skipping duplicate backtest cache persistence for run_id=%s; canonical run_id=%s",
        run_id,
        existing[0],
    )
    return False


def save_backtest_output(
    output: BacktestOutput,
    *,
    signal_series_by_symbol: Mapping[str, pd.Series] | None = None,
    exit_signal_series_by_symbol: Mapping[str, pd.Series] | None = None,
    params: dict | None = None,
    execution_policy: dict | None = None,
    risk_policy: dict | None = None,
    config_hash: str | None = None,
    data_source_by_symbol: Mapping[str, str] | None = None,
    backtest_revision: str | None = None,
    replace_existing: bool = False,
    bars_by_symbol: Mapping[str, pd.DataFrame] | None = None,
    dsn: str | None = None,
) -> dict:
    """Write a complete BacktestOutput to TimescaleDB.

    Args:
        output: BacktestOutput to write.
        signal_series_by_symbol: Optional entry-signal series keyed by symbol.
        exit_signal_series_by_symbol: Optional exit-signal series keyed by symbol.
        params: Optional strategy_name parameters dict to store as JSONB.
        execution_policy: Resolved fill and liquidity assumptions to store.
        risk_policy: Resolved engine-level portfolio limits to store.
        config_hash: Deterministic engine-configuration hash.
        data_source_by_symbol: Exact raw-data source used for each run symbol.
        backtest_revision: Optional caller-owned strategy_name-code and input-data
            revision. Cache reuse is disabled when omitted.
        replace_existing: Replace the canonical run for the derived cache key.
        bars_by_symbol: Input bars to keep in ``run_bars`` for this run's
            charts, one frame per run symbol. Omitted, the run keeps no bars:
            its metadata already names the exact subscriptions and range it used.
        dsn: TimescaleDB DSN.

    Returns:
        Dict mapping table names to row counts written.
    """
    output.validate()
    meta = output.run_metadata
    # These saves clear a run's rows before writing; a sim or live run's rows
    # are the only record of what its engine saw and did.
    if meta.mode != "backtest":
        raise ValueError(f"save_backtest_output stores backtest runs, not mode={meta.mode!r}")
    auxiliary_subscriptions = getattr(meta, "auxiliary_subscriptions", ())
    if isinstance(auxiliary_subscriptions, tuple) and auxiliary_subscriptions:
        raise ValueError(
            "TimescaleDB run persistence does not yet store auxiliary_subscriptions; "
            "use a backtest artifact or omit auxiliary_data"
        )
    counts: dict[str, int] = {}
    revision = normalize_backtest_revision(backtest_revision)
    backtest_cache_key = build_backtest_cache_key(config_hash, revision)
    if replace_existing and backtest_cache_key is None:
        raise ValueError("replace_existing=True requires backtest_revision")
    subscription_by_symbol = {item.symbol: item for item in meta.primary_subscriptions}
    # All or none: a run with bars for some symbols reads as fully priced while
    # its unpriced positions drop out of every mark, and a later duplicate
    # asking for bars would be skipped as if they were all there.
    if bars_by_symbol is not None:
        if set(bars_by_symbol) != set(subscription_by_symbol):
            raise ValueError(
                "bars_by_symbol must cover exactly the run symbols; "
                f"missing={sorted(set(subscription_by_symbol) - set(bars_by_symbol))}, "
                f"extra={sorted(set(bars_by_symbol) - set(subscription_by_symbol))}"
            )
        empty = sorted(symbol for symbol, bars in bars_by_symbol.items() if bars.empty)
        if empty:
            raise ValueError(f"bars_by_symbol has no bars for {empty}")

    # WHY: single transaction for atomicity — partial writes on failure
    # would leave DB in inconsistent state (e.g. trades without metadata).
    with get_conn(dsn) as conn:
        cur = conn.cursor()

        if not _claim_backtest_cache_key(
            cur,
            backtest_cache_key,
            meta.run_id,
            replace_existing=replace_existing,
            store_bars=bars_by_symbol is not None,
        ):
            cur.close()
            return {"backtest_runs": 0}

        write_run_metadata(
            run_id=meta.run_id,
            strategy_name=meta.strategy_name,
            symbols=meta.symbols,
            timeframe=meta.timeframe,
            mode=meta.mode,
            started_at=meta.started_at,
            ended_at=meta.ended_at,
            run_at=meta.run_at,
            data_source=meta.data_source,
            data_source_by_symbol=data_source_by_symbol,
            session_mode=meta.session_mode,
            primary_subscriptions=meta.primary_subscriptions,
            params=params,
            execution_policy=execution_policy,
            risk_policy=risk_policy,
            config_hash=config_hash,
            backtest_revision=revision,
            backtest_cache_key=backtest_cache_key,
            cur=cur,
        )
        counts["backtest_runs"] = 1

        tf = to_canonical(meta.timeframe)

        # Clear old data (idempotent re-run)
        cur.execute("DELETE FROM equity_curve WHERE run_id = %s", (meta.run_id,))
        cur.execute("DELETE FROM position_events WHERE run_id = %s", (meta.run_id,))
        cur.execute("DELETE FROM financing_cash_flows WHERE run_id = %s", (meta.run_id,))
        cur.execute("DELETE FROM strategy_performance WHERE run_id = %s", (meta.run_id,))
        cur.execute("DELETE FROM run_bars WHERE run_id = %s", (meta.run_id,))

        if bars_by_symbol:
            counts["run_bars"] = sum(
                write_run_bars(meta.run_id, subscription_by_symbol[symbol], bars, cur=cur)
                for symbol, bars in bars_by_symbol.items()
            )

        # equity_curve (batch)
        account = output.account
        if account.equity_curve:
            eq_rows = [
                (
                    _to_dt(eq.ts),
                    meta.run_id,
                    account.account_id,
                    account.currency,
                    eq.equity,
                    eq.drawdown,
                    eq.period_return,
                    eq.gross_exposure,
                    eq.net_exposure,
                    eq.concentration,
                    eq.turnover,
                    eq.exposed,
                    meta.strategy_name,
                )
                for eq in account.equity_curve
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO equity_curve
                   (ts, run_id, account_id, currency,
                    equity, drawdown, period_return,
                    gross_exposure, net_exposure,
                    concentration, turnover, exposed, strategy_name)
                   VALUES %s""",
                eq_rows,
                page_size=1000,
            )
            counts["equity_curve"] = len(eq_rows)

        # position_events (batch)
        if output.position_events:
            event_rows = [
                (
                    ev.event_id,
                    meta.run_id,
                    ev.account_id,
                    ev.currency,
                    meta.strategy_name,
                    meta.mode,
                    tf,
                    _to_dt(ev.ts),
                    ev.symbol,
                    ev.side,
                    ev.event_type,
                    ev.fill_quantity,
                    ev.price,
                    ev.entry_price,
                    ev.remaining_quantity,
                    ev.notional,
                    ev.commission,
                    ev.slippage,
                    ev.tax,
                    ev.entry_commission,
                    ev.entry_slippage,
                    ev.entry_tax,
                    ev.realized_pnl,
                    ev.net_return,
                    _to_dt(ev.entry_at) if ev.entry_at else None,
                    ev.periods_held,
                    ev.reason,
                    ev.group_id,
                    ev.time_in_force,
                    ev.margin_locked,
                    ev.leverage,
                    ev.liquidation_price,
                    ev.margin_roi,
                    ev.margin_mode,
                    ev.cash_flow,
                )
                for ev in output.position_events
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO position_events
                   (event_id, run_id, account_id, currency,
                    strategy_name, mode, timeframe,
                    ts, symbol, side, event_type,
                    fill_quantity, price, entry_price,
                    remaining_quantity, notional,
                    commission, slippage, tax,
                    entry_commission, entry_slippage, entry_tax,
                    realized_pnl, net_return,
                    entry_at, periods_held, reason, group_id, time_in_force,
                    margin_locked, leverage, liquidation_price, margin_roi, margin_mode,
                    cash_flow)
                   VALUES %s
                   ON CONFLICT (event_id, ts) DO NOTHING""",
                event_rows,
                page_size=500,
            )
            counts["position_events"] = len(event_rows)

        if output.financing_cash_flows:
            funding_rows = [
                (
                    _to_dt(cash_flow.ts),
                    meta.run_id,
                    cash_flow.account_id,
                    cash_flow.currency,
                    cash_flow.symbol,
                    cash_flow.kind,
                    cash_flow.side,
                    cash_flow.quantity,
                    cash_flow.mark_price,
                    cash_flow.multiplier,
                    cash_flow.rate,
                    cash_flow.cash_flow,
                    cash_flow.group_id,
                    _to_dt(cash_flow.entry_at),
                )
                for cash_flow in output.financing_cash_flows
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO financing_cash_flows
                   (ts, run_id, account_id, currency, symbol, kind, side,
                    quantity, mark_price, multiplier, rate, cash_flow,
                    group_id, entry_at)
                   VALUES %s
                   ON CONFLICT (run_id, account_id, symbol, kind, ts) DO UPDATE SET
                     currency=EXCLUDED.currency,
                     side=EXCLUDED.side,
                     quantity=EXCLUDED.quantity,
                     mark_price=EXCLUDED.mark_price,
                     multiplier=EXCLUDED.multiplier,
                     rate=EXCLUDED.rate,
                     cash_flow=EXCLUDED.cash_flow,
                     group_id=EXCLUDED.group_id,
                     entry_at=EXCLUDED.entry_at""",
                funding_rows,
                page_size=500,
            )
            counts["financing_cash_flows"] = len(funding_rows)

        # signal_events (from feature-layer signal_series)
        entry_signals = dict(signal_series_by_symbol or {})
        exit_signals = dict(exit_signal_series_by_symbol or {})

        sig_count = 0
        signal_symbols = set(entry_signals) | set(exit_signals)
        for signal_symbol in sorted(signal_symbols):
            entries = entry_signals.get(signal_symbol)
            exits = exit_signals.get(signal_symbol)
            cur.execute(
                """DELETE FROM signal_events
                   WHERE run_id = %s AND strategy_name = %s AND symbol = %s AND mode = 'backtest'
                     AND timeframe = %s
                     AND ts BETWEEN %s AND %s""",
                (
                    meta.run_id,
                    meta.strategy_name,
                    signal_symbol,
                    tf,
                    _to_dt(meta.started_at),
                    _to_dt(meta.ended_at),
                ),
            )
            if entries is not None and not entries.empty:
                entry_rows = [
                    (
                        _to_dt(ts),
                        meta.run_id,
                        meta.strategy_name,
                        signal_symbol,
                        "backtest",
                        tf,
                        float(val),
                        None,
                        "entry",
                    )
                    for ts, val in entries.items()
                ]
                psycopg2.extras.execute_values(
                    cur,
                    _SIGNAL_INSERT_SQL,
                    entry_rows,
                    page_size=1000,
                )
                sig_count += len(entry_rows)
            if exits is not None and not exits.empty:
                exit_rows = [
                    (
                        _to_dt(ts),
                        meta.run_id,
                        meta.strategy_name,
                        signal_symbol,
                        "backtest",
                        tf,
                        float(val),
                        None,
                        "exit",
                    )
                    for ts, val in exits.items()
                ]
                psycopg2.extras.execute_values(
                    cur,
                    _SIGNAL_INSERT_SQL,
                    exit_rows,
                    page_size=1000,
                )
                sig_count += len(exit_rows)
        if sig_count:
            counts["signal_events"] = sig_count

        write_strategy_performance(
            meta.run_id,
            account.account_id,
            account.currency,
            account.initial_cash,
            account.final_equity,
            account.net_pnl,
            account.metrics,
            cur=cur,
        )
        counts["strategy_performance"] = 1

        cur.close()

    return counts


_RUN_BARS_INSERT_SQL = """INSERT INTO run_bars (
           ts, run_id, symbol, timeframe, calendar_id, session_mode,
           data_source, instrument_type, available_at, observed_at,
           open, high, low, close, volume)
       VALUES %s
       ON CONFLICT (run_id, symbol, ts, available_at, open, high, low, close, volume)
           DO NOTHING
       RETURNING 1"""


def write_run_bars(
    run_id: str,
    subscription: MarketDataSubscription,
    bars: pd.DataFrame,
    *,
    cur: PgCursor | None = None,
    dsn: str | None = None,
) -> int:
    """Record bars that one run saw for one of its subscriptions.

    ``bars`` has a DatetimeIndex or a ``ts`` column, the OHLCV columns, and
    optionally ``available_at`` (derived only when the calendar proves the
    bar's close) and ``observed_at`` (stored NULL when absent, as for a
    backtest's inputs). Each version, ``(ts, available_at)`` with the bar's
    values, is stored once: a repeat is a no-op that keeps the first
    ``observed_at``, so a replayed delivery cannot move when a version was
    received, while revised values under an unchanged ``available_at`` are a
    version of their own. Readers order a bar's versions by ``observed_at``,
    then ``available_at``, so two value sets written with both equal cannot be
    told apart; the engine never writes such a pair.

    The whole batch is refused, with ``BarTimestampError``, if any ``ts`` is
    not a bar start on the subscription's calendar (``validate_bar_starts``).

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction). Returns the number of versions inserted.
    """
    if not isinstance(subscription, MarketDataSubscription):
        raise TypeError("subscription must be a MarketDataSubscription")
    if bars is None or bars.empty:
        return 0

    validate_ohlcv_values(bars, context="run bars write")
    if "ts" not in bars.columns:
        if not isinstance(bars.index, pd.DatetimeIndex):
            raise ValueError("run bars write requires a ts column or DatetimeIndex")
        bars = bars.rename_axis("ts").reset_index()
    validate_bar_starts(
        bars["ts"],
        subscription.timeframe,
        subscription.calendar_id,
        session_mode=subscription.session_mode,
        context=f"run bars write for {subscription.symbol!r} {subscription.data_source!r}",
    )
    bars = bars.sort_values("ts", kind="stable").reset_index(drop=True)
    timestamps, available_at = normalize_bar_times(
        bars["ts"],
        bars.get(AVAILABLE_AT_COLUMN),
        subscription,
    )
    if timestamps.duplicated().any():
        raise ValueError("run bars write requires unique timestamps per subscription")
    observed_at = (
        [_to_dt(value) for value in bars[OBSERVED_AT_COLUMN]]
        if OBSERVED_AT_COLUMN in bars
        else [None] * len(bars)
    )

    rows = list(
        zip(
            timestamps.to_pydatetime(),
            [run_id] * len(bars),
            [subscription.symbol] * len(bars),
            [subscription.timeframe] * len(bars),
            [subscription.calendar_id] * len(bars),
            [subscription.session_mode] * len(bars),
            [subscription.data_source] * len(bars),
            [subscription.instrument_type] * len(bars),
            available_at.to_pydatetime(),
            observed_at,
            bars["open"].astype(float),
            bars["high"].astype(float),
            bars["low"].astype(float),
            bars["close"].astype(float),
            bars["volume"].astype(float),
            strict=True,
        )
    )
    if cur is not None:
        return len(
            psycopg2.extras.execute_values(
                cur, _RUN_BARS_INSERT_SQL, rows, page_size=2000, fetch=True
            )
        )
    with get_conn(dsn) as conn:
        c = conn.cursor()
        inserted = psycopg2.extras.execute_values(
            c, _RUN_BARS_INSERT_SQL, rows, page_size=2000, fetch=True
        )
        c.close()
    return len(inserted)


def _input_bars(symbol_df: pd.DataFrame) -> pd.DataFrame:
    """The bar columns of one symbol's backtest input, for ``write_run_bars``."""
    columns = list(OHLCV_COLUMNS)
    if AVAILABLE_AT_COLUMN in symbol_df:
        columns.append(AVAILABLE_AT_COLUMN)
    return symbol_df[columns].rename_axis("ts")


def write_signal_event(
    ts: datetime,
    run_id: str,
    strategy_name: str,
    symbol: str,
    mode: str,
    timeframe: str,
    signal_value: float,
    price: float | None = None,
    signal_type: str = "entry",
    *,
    cur: PgCursor | None = None,
    dsn: str | None = None,
) -> None:
    """Write a single signal event row (upsert, idempotent).

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction).  Otherwise opens its own connection and commits.
    """
    sql = """INSERT INTO signal_events
               (ts, run_id, strategy_name, symbol, mode, timeframe, signal_value, price, signal_type)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (ts, run_id, strategy_name, symbol, mode, timeframe, signal_type)
               DO NOTHING"""
    values = (
        _to_dt(ts),
        run_id,
        strategy_name,
        symbol,
        mode,
        timeframe,
        signal_value,
        price,
        signal_type,
    )
    if cur is not None:
        cur.execute(sql, values)
    else:
        with get_conn(dsn) as conn:
            c = conn.cursor()
            c.execute(sql, values)
            c.close()


def write_equity_curve_point(
    ts: datetime,
    run_id: str,
    account_id: str,
    currency: str,
    equity: float,
    strategy_name: str,
    drawdown: float = 0.0,
    period_return: float = 0.0,
    gross_exposure: float | None = None,
    net_exposure: float | None = None,
    concentration: float | None = None,
    turnover: float | None = None,
    exposed: bool | None = None,
    dsn: str | None = None,
) -> None:
    """Write one account equity point."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO equity_curve
               (ts, run_id, account_id, currency,
                equity, drawdown, period_return, gross_exposure, net_exposure,
                concentration, turnover, exposed, strategy_name)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, account_id, ts) DO UPDATE SET
                 currency=EXCLUDED.currency,
                 equity=EXCLUDED.equity,
                 drawdown=EXCLUDED.drawdown, period_return=EXCLUDED.period_return,
                 gross_exposure=EXCLUDED.gross_exposure,
                 net_exposure=EXCLUDED.net_exposure,
                 concentration=EXCLUDED.concentration,
                 turnover=EXCLUDED.turnover,
                 exposed=EXCLUDED.exposed,
                 strategy_name=EXCLUDED.strategy_name""",
            (
                _to_dt(ts),
                run_id,
                account_id,
                currency,
                equity,
                drawdown,
                period_return,
                gross_exposure,
                net_exposure,
                concentration,
                turnover,
                exposed,
                strategy_name,
            ),
        )
        cur.close()


def write_trade_event(
    event_id: str,
    run_id: str,
    account_id: str,
    currency: str,
    strategy_name: str,
    mode: str,
    timeframe: str,
    ts: datetime,
    symbol: str,
    side: str,
    event_type: str,
    fill_quantity: float,
    price: float,
    entry_price: float,
    remaining_quantity: float,
    notional: float,
    commission: float = 0.0,
    slippage: float = 0.0,
    tax: float = 0.0,
    realized_pnl: float | None = None,
    net_return: float | None = None,
    entry_at: datetime | None = None,
    periods_held: int | None = None,
    reason: str = "",
    entry_commission: float | None = None,
    entry_slippage: float | None = None,
    entry_tax: float | None = None,
    group_id: str | None = None,
    time_in_force: str | None = None,
    margin_locked: float | None = None,
    leverage: float | None = None,
    liquidation_price: float | None = None,
    margin_roi: float | None = None,
    margin_mode: str | None = None,
    cash_flow: float | None = None,
    dsn: str | None = None,
) -> None:
    """Write a single trade event (upsert by event_id + ts)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO position_events
               (event_id, run_id, account_id, currency,
                strategy_name, mode, timeframe,
                ts, symbol, side, event_type,
                fill_quantity, price, entry_price,
                remaining_quantity, notional,
                commission, slippage, tax,
                entry_commission, entry_slippage, entry_tax,
                realized_pnl, net_return,
                entry_at, periods_held, reason, group_id, time_in_force,
                margin_locked, leverage, liquidation_price, margin_roi, margin_mode,
                cash_flow)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (event_id, ts) DO NOTHING""",
            (
                event_id,
                run_id,
                account_id,
                currency,
                strategy_name,
                mode,
                timeframe,
                _to_dt(ts),
                symbol,
                side,
                event_type,
                fill_quantity,
                price,
                entry_price,
                remaining_quantity,
                notional,
                commission,
                slippage,
                tax,
                entry_commission,
                entry_slippage,
                entry_tax,
                realized_pnl,
                net_return,
                _to_dt(entry_at) if entry_at else None,
                periods_held,
                reason,
                group_id,
                time_in_force,
                margin_locked,
                leverage,
                liquidation_price,
                margin_roi,
                margin_mode,
                cash_flow,
            ),
        )
        cur.close()


def write_financing_cash_flow(
    run_id: str,
    account_id: str,
    currency: str,
    ts: datetime,
    symbol: str,
    kind: str,
    side: str,
    quantity: float,
    mark_price: float,
    multiplier: float,
    rate: float,
    cash_flow: float,
    group_id: str | None,
    entry_at: datetime,
    dsn: str | None = None,
) -> None:
    """Write one idempotent financing payment."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO financing_cash_flows
               (ts, run_id, account_id, currency, symbol, kind, side,
                quantity, mark_price, multiplier, rate, cash_flow,
                group_id, entry_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, account_id, symbol, kind, ts) DO UPDATE SET
                 currency=EXCLUDED.currency,
                 side=EXCLUDED.side,
                 quantity=EXCLUDED.quantity,
                 mark_price=EXCLUDED.mark_price,
                 multiplier=EXCLUDED.multiplier,
                 rate=EXCLUDED.rate,
                 cash_flow=EXCLUDED.cash_flow,
                 group_id=EXCLUDED.group_id,
                 entry_at=EXCLUDED.entry_at""",
            (
                _to_dt(ts),
                run_id,
                account_id,
                currency,
                symbol,
                kind,
                side,
                quantity,
                mark_price,
                multiplier,
                rate,
                cash_flow,
                group_id,
                _to_dt(entry_at),
            ),
        )
        cur.close()


def write_runtime_event(
    run_id: str,
    ts: datetime,
    event_type: str,
    symbol: str | None = None,
    detail: Mapping[str, object] | None = None,
    dsn: str | None = None,
) -> None:
    """Write one idempotent operational-audit event (not a fill)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO runtime_events (ts, run_id, event_type, symbol, detail)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, ts, event_type, COALESCE(symbol, '')) DO UPDATE SET
                 detail=EXCLUDED.detail""",
            (
                _to_dt(ts),
                run_id,
                event_type,
                symbol,
                json.dumps(_storable_json(detail)) if detail is not None else None,
            ),
        )
        cur.close()


def _storable_json(value: object) -> object:
    """``value`` with every string made storable; a halt's detail quotes venue errors."""
    if isinstance(value, str):
        return _storable_text(value)
    if isinstance(value, Mapping):
        return {_storable_json(key): _storable_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_storable_json(item) for item in value]
    return value


def write_strategy_performance(
    run_id: str,
    account_id: str,
    currency: str,
    initial_cash: float,
    final_equity: float,
    net_pnl: float,
    metrics: Any,
    *,
    if_absent: bool = False,
    cur: PgCursor | None = None,
    dsn: str | None = None,
) -> None:
    """Write/update one account's strategy_name performance.

    ``if_absent=True`` writes only when the account has no row yet, for a
    baseline that must not replace KPIs already computed for the run.

    If ``cur`` is provided, executes on that cursor (caller owns the
    transaction).  Otherwise opens its own connection and commits.
    """
    sql = """INSERT INTO strategy_performance
               (run_id, account_id, currency, initial_cash, final_equity, net_pnl,
                total_return, mean_period_return, period_volatility,
                period_downside_deviation, period_sharpe, period_sortino,
                positive_period_rate,
                max_drawdown, win_rate, profit_factor, payoff_ratio, trades,
                avg_trade_return, exposure_ratio, total_turnover,
                average_gross_exposure, max_gross_exposure,
                max_abs_net_exposure, max_concentration,
                total_commission, total_slippage, total_tax)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, account_id) DO """
    sql += (
        "NOTHING"
        if if_absent
        else """UPDATE SET
                 currency=EXCLUDED.currency,
                 initial_cash=EXCLUDED.initial_cash,
                 final_equity=EXCLUDED.final_equity,
                 net_pnl=EXCLUDED.net_pnl,
                 total_return=EXCLUDED.total_return,
                 mean_period_return=EXCLUDED.mean_period_return,
                 period_volatility=EXCLUDED.period_volatility,
                 period_downside_deviation=EXCLUDED.period_downside_deviation,
                 period_sharpe=EXCLUDED.period_sharpe,
                 period_sortino=EXCLUDED.period_sortino,
                 positive_period_rate=EXCLUDED.positive_period_rate,
                 max_drawdown=EXCLUDED.max_drawdown,
                 win_rate=EXCLUDED.win_rate,
                 profit_factor=EXCLUDED.profit_factor,
                 payoff_ratio=EXCLUDED.payoff_ratio,
                 trades=EXCLUDED.trades,
                 avg_trade_return=EXCLUDED.avg_trade_return,
                 exposure_ratio=EXCLUDED.exposure_ratio,
                 total_turnover=EXCLUDED.total_turnover,
                 average_gross_exposure=EXCLUDED.average_gross_exposure,
                 max_gross_exposure=EXCLUDED.max_gross_exposure,
                 max_abs_net_exposure=EXCLUDED.max_abs_net_exposure,
                 max_concentration=EXCLUDED.max_concentration,
                 total_commission=EXCLUDED.total_commission,
                 total_slippage=EXCLUDED.total_slippage,
                 total_tax=EXCLUDED.total_tax"""
    )
    params = (
        run_id,
        account_id,
        currency,
        initial_cash,
        final_equity,
        net_pnl,
        metrics.total_return,
        metrics.mean_period_return,
        metrics.period_volatility,
        metrics.period_downside_deviation,
        metrics.period_sharpe,
        metrics.period_sortino,
        metrics.positive_period_rate,
        metrics.max_drawdown,
        metrics.win_rate,
        metrics.profit_factor,
        metrics.payoff_ratio,
        metrics.trades,
        metrics.avg_trade_return,
        metrics.exposure_ratio,
        metrics.total_turnover,
        metrics.average_gross_exposure,
        metrics.max_gross_exposure,
        metrics.max_abs_net_exposure,
        metrics.max_concentration,
        metrics.total_commission,
        metrics.total_slippage,
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
    account_id: str,
    config: RunConfig | None = None,
    dsn: str | None = None,
) -> None:
    """Recompute KPI metrics from DB equity_curve + position_events and upsert.

    The live engine calls it after a close, reduce or financing cash flow, and
    on the first equity point after a restore, to keep the KPIs current.
    Only the upsert is bounded (``_bounded_statements``): the reads cover the
    whole run, so a long run may legitimately take longer than the bound.
    """
    from librae.core import EPSILON
    from librae.core.executor import REASON_FORCE_CLOSE, TradePnL
    from librae.core.financing import (
        FinancingCashFlow,
        FinancingLifecycleEvent,
        attribute_financing_to_closes,
    )
    from librae.db.timescale_reader import (
        load_equity_curve,
        load_financing_cash_flows,
        load_position_events,
    )

    _CLOSE_TYPES = ["close", "reduce"]
    from librae.core.metrics import compute_all

    eq_df = load_equity_curve(run_id, account_id=account_id)
    if eq_df.empty or len(eq_df) < 2:
        return

    events_df = load_position_events(run_id, account_id=account_id)
    event_rows = events_df.to_dict("records") if not events_df.empty else []
    trade_rows = [
        row
        for row in event_rows
        if row.get("event_type") in _CLOSE_TYPES or "event_type" not in row
    ]
    required_trade_fields = (
        "realized_pnl",
        "commission",
        "slippage",
        "tax",
        "entry_commission",
        "entry_slippage",
        "entry_tax",
        "net_return",
        "fill_quantity",
        "price",
        "entry_price",
        "notional",
        "entry_at",
    )
    for row in trade_rows:
        missing = [
            field for field in required_trade_fields if field not in row or pd.isna(row[field])
        ]
        if missing:
            raise ValueError(
                "closed trade event missing required performance fields: " + ", ".join(missing)
            )

    funding_df = load_financing_cash_flows(run_id, account_id=account_id)
    funding_rows = funding_df.to_dict("records") if not funding_df.empty else []
    financing_by_close = [0.0] * len(trade_rows)
    if funding_rows:
        required_lifecycle_fields = (
            "_time",
            "symbol",
            "event_type",
            "fill_quantity",
            "remaining_quantity",
            "entry_at",
        )
        for row in event_rows:
            missing = [
                field
                for field in required_lifecycle_fields
                if field not in row or pd.isna(row[field])
            ]
            if missing:
                raise ValueError(
                    "position event missing required financing fields: " + ", ".join(missing)
                )
        lifecycle_events = [
            FinancingLifecycleEvent(
                ts=row["_time"],
                symbol=str(row["symbol"]),
                entry_at=row["entry_at"],
                event_type=row["event_type"],
                fill_quantity=float(row["fill_quantity"]),
                remaining_quantity=float(row["remaining_quantity"]),
                # Same ordering the engine applies in memory, read back off the
                # stored reason — otherwise a run that attributes in memory
                # fails once persisted.
                after_bar_accrual=row.get("reason") == REASON_FORCE_CLOSE,
            )
            for row in event_rows
        ]
        cash_flows = [
            FinancingCashFlow(
                ts=row["_time"],
                symbol=str(row["symbol"]),
                side=row["side"],
                quantity=float(row["quantity"]),
                mark_price=float(row["mark_price"]),
                multiplier=float(row["multiplier"]),
                rate=float(row["rate"]),
                cash_flow=float(row["cash_flow"]),
                group_id=row.get("group_id") if pd.notna(row.get("group_id")) else None,
                entry_at=row["entry_at"],
                kind=row["kind"],
            )
            for row in funding_rows
        ]
        financing_by_close = attribute_financing_to_closes(lifecycle_events, cash_flows)

    # WHY: compute_all accepts primitive sequences — build them from DB rows
    eq_records = eq_df.to_dict("records")
    equity_values = [float(r["equity"]) for r in eq_records]
    timestamps = [r["_time"] for r in eq_records]
    trade_notionals = [
        abs(float(r["notional"] * r["entry_price"] / r["price"])) for r in trade_rows
    ]
    trade_pnls = []
    for r, notional, financing_pnl in zip(
        trade_rows,
        trade_notionals,
        financing_by_close,
        strict=True,
    ):
        financing_return = financing_pnl / notional * 100.0 if notional > EPSILON else 0.0
        trade_pnls.append(
            TradePnL(
                gross_pnl=float(
                    r["realized_pnl"]
                    + r["entry_commission"]
                    + r["entry_slippage"]
                    + r["entry_tax"]
                    + r["commission"]
                    + r["slippage"]
                    + r["tax"]
                    + financing_pnl
                ),
                net_pnl=float(r["realized_pnl"]) + financing_pnl,
                commission=float(r["entry_commission"] + r["commission"]),
                slippage=float(r["entry_slippage"] + r["slippage"]),
                tax=float(r["entry_tax"] + r["tax"]),
                gross_return=financing_return,
                net_return=float(r["net_return"]) + financing_return,
                exit_commission=0.0,
                exit_slippage=0.0,
                exit_tax=0.0,
            )
        )
    trade_group_ids = [
        r.get("group_id") if pd.notna(r.get("group_id")) else None for r in trade_rows
    ]
    trade_entry_ats = [r["entry_at"] for r in trade_rows]

    def complete_optional_series(field: str) -> list[float] | None:
        values = [row.get(field) for row in eq_records]
        if not values or any(pd.isna(value) for value in values):
            return None
        return [float(value) for value in values]

    exposed_values = [row.get("exposed") for row in eq_records]
    exposed_periods = (
        sum(bool(value) for value in exposed_values)
        if exposed_values and not any(pd.isna(value) for value in exposed_values)
        else None
    )

    metrics = compute_all(
        equity_values=equity_values,
        timestamps=timestamps,
        trade_pnls=trade_pnls,
        total_periods=len(equity_values),
        exposed_periods=exposed_periods,
        trade_notionals=trade_notionals,
        trade_group_ids=trade_group_ids,
        trade_entry_ats=trade_entry_ats,
        turnover_values=complete_optional_series("turnover"),
        gross_exposure_values=complete_optional_series("gross_exposure"),
        net_exposure_values=complete_optional_series("net_exposure"),
        concentration_values=complete_optional_series("concentration"),
    )
    if config is None:
        raise ValueError("config is required to refresh account performance")
    if account_id != config.account_id:
        raise ValueError(f"unknown account_id: {account_id!r}")
    account = config.account
    final_equity = equity_values[-1]
    with _bounded_statements():
        write_strategy_performance(
            run_id,
            account_id,
            account.currency,
            account.initial_cash,
            final_equity,
            final_equity - account.initial_cash,
            metrics,
            dsn=dsn,
        )


def save_signal_results(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    strategy_name: str,
    data_source: str,
    run_id: str | None = None,
    mode: str = "backtest",
    signal_column: str = "entry_signal",
    config: RunConfig | None = None,
    primary_subscription: MarketDataSubscription | None = None,
    replace_existing: bool = False,
    backtest_revision: str | None = None,
    store_bars: bool = False,
) -> dict:
    """Write signal history to DB. Independent of backtest engine.

    Use this for signal quality analysis without running a full backtest.
    When config is provided, stores its execution identity and policies.
    ``store_bars`` also keeps the input bars in ``run_bars``, which the
    console's price charts and signal outcomes read; it needs a ``run_id``.
    """
    if store_bars and run_id is None:
        raise ValueError("store_bars=True requires run_id: run_bars rows belong to a run")
    if run_id is not None and mode != "backtest":
        raise ValueError(
            f"save_signal_results stores backtest runs; a run_id with mode={mode!r} would "
            "clear rows an engine recorded"
        )
    symbol_df, signal_series = _extract_signals(df, symbol, signal_column)
    exit_signal_series = _extract_exit_signals(df, symbol)
    tf = to_canonical(timeframe)
    if config is not None:
        from librae.config.symbols import resolve_symbol

        resolved_subscription = subscription_from_instrument(
            resolve_symbol(config, symbol),
            timeframe=tf,
            session_mode=config.session_mode,
        )
        if primary_subscription is not None and primary_subscription != resolved_subscription:
            raise ValueError(
                "primary_subscription does not match the identity resolved from config"
            )
    else:
        if primary_subscription is None:
            raise ValueError("primary_subscription is required when config is not provided")
        resolved_subscription = primary_subscription
    if (
        resolved_subscription.symbol != symbol
        or resolved_subscription.timeframe != tf
        or resolved_subscription.data_source != data_source
    ):
        raise ValueError(
            "primary_subscription must match symbol, timeframe, and data_source arguments"
        )
    counts: dict[str, int] = {}
    revision = normalize_backtest_revision(backtest_revision)
    config_hash = config.config_hash if config else None
    backtest_cache_key = build_backtest_cache_key(config_hash, revision)
    if replace_existing and backtest_cache_key is None:
        raise ValueError("replace_existing=True requires backtest_revision")

    has_entry = not signal_series.empty
    has_exit = not exit_signal_series.empty
    if store_bars and not (has_entry or has_exit):
        # The run's range comes from its signals; without one nothing is written.
        raise ValueError("store_bars=True needs at least one signal to store a run")
    if has_entry or has_exit:
        started_at = min(
            signal_series.index.min() if has_entry else exit_signal_series.index.min(),
            exit_signal_series.index.min() if has_exit else signal_series.index.min(),
        )
        ended_at = max(
            signal_series.index.max() if has_entry else exit_signal_series.index.max(),
            exit_signal_series.index.max() if has_exit else signal_series.index.max(),
        )
        sig_count = 0

        with get_conn() as conn:
            cur = conn.cursor()

            if run_id is not None:
                if not _claim_backtest_cache_key(
                    cur,
                    backtest_cache_key,
                    run_id,
                    replace_existing=replace_existing,
                    store_bars=store_bars,
                ):
                    cur.close()
                    return {"backtest_runs": 0}
                write_run_metadata(
                    run_id,
                    strategy_name,
                    [symbol],
                    tf,
                    mode,
                    started_at=_to_dt(started_at),
                    ended_at=_to_dt(ended_at),
                    data_source=data_source,
                    data_source_by_symbol={symbol: data_source},
                    session_mode=resolved_subscription.session_mode,
                    primary_subscriptions=(resolved_subscription,),
                    config_hash=config_hash,
                    backtest_revision=revision,
                    backtest_cache_key=backtest_cache_key,
                    params=config.params if config else None,
                    execution_policy=asdict(config.execution) if config else None,
                    risk_policy=asdict(config.risk) if config else None,
                    cur=cur,
                )

            # run_id may be None here (ad-hoc signal-quality analysis without
            # a full backtest run) — "IS NOT DISTINCT FROM" matches NULL = NULL
            # too, so that case still only clears its own prior (run_id=NULL)
            # rows, not every run_id's.
            cur.execute(
                """DELETE FROM signal_events
                   WHERE run_id IS NOT DISTINCT FROM %s
                     AND strategy_name = %s AND symbol = %s AND mode = %s
                     AND timeframe = %s
                     AND ts BETWEEN %s AND %s""",
                (run_id, strategy_name, symbol, mode, tf, _to_dt(started_at), _to_dt(ended_at)),
            )
            if has_entry:
                entry_rows = [
                    (_to_dt(ts), run_id, strategy_name, symbol, mode, tf, float(val), None, "entry")
                    for ts, val in signal_series.items()
                ]
                psycopg2.extras.execute_values(cur, _SIGNAL_INSERT_SQL, entry_rows, page_size=1000)
                sig_count += len(entry_rows)
            if has_exit:
                exit_rows = [
                    (_to_dt(ts), run_id, strategy_name, symbol, mode, tf, float(val), None, "exit")
                    for ts, val in exit_signal_series.items()
                ]
                psycopg2.extras.execute_values(cur, _SIGNAL_INSERT_SQL, exit_rows, page_size=1000)
                sig_count += len(exit_rows)
            counts["signal_events"] = sig_count

            if run_id is not None:
                cur.execute("DELETE FROM run_bars WHERE run_id = %s", (run_id,))
                if store_bars:
                    counts["run_bars"] = write_run_bars(
                        run_id, resolved_subscription, _input_bars(symbol_df), cur=cur
                    )
            cur.close()

    return counts


def save_strategy_results(
    output: BacktestOutput,
    df: pd.DataFrame,
    config: RunConfig,
    signal_column: str = "entry_signal",
    *,
    replace_existing: bool = False,
    backtest_revision: str | None = None,
    store_bars: bool = False,
) -> dict:
    """Write strategy_name backtest results + signal history to DB.

    Writes: backtest_runs, equity_curve, position_events, financing_cash_flows,
    strategy_performance, signal_events. The input bars are not copied: the
    run's metadata names the exact subscriptions and range it used.
    ``store_bars=True`` also keeps them in ``run_bars``, for price charts.
    """
    from librae.config.symbols import resolve_symbol

    instruments = {symbol: resolve_symbol(config, symbol) for symbol in config.symbols}
    subscriptions = tuple(
        subscription_from_instrument(
            instruments[symbol],
            timeframe=config.timeframe,
            session_mode=config.session_mode,
        )
        for symbol in config.symbols
    )
    if output.run_metadata.primary_subscriptions != subscriptions:
        raise ValueError(
            "output primary_subscriptions do not match identities resolved from config"
        )
    data_source_by_symbol = {
        symbol: instrument.data_source for symbol, instrument in instruments.items()
    }

    signal_series_by_symbol: dict[str, pd.Series] = {}
    exit_signal_series_by_symbol: dict[str, pd.Series] = {}
    symbol_frames: dict[str, pd.DataFrame] = {}
    for symbol in config.symbols:
        symbol_df, signal_series = _extract_signals(df, symbol, signal_column)
        symbol_frames[symbol] = symbol_df
        signal_series_by_symbol[symbol] = signal_series
        exit_signal_series_by_symbol[symbol] = _extract_exit_signals(df, symbol)

    return save_backtest_output(
        output,
        signal_series_by_symbol=signal_series_by_symbol,
        exit_signal_series_by_symbol=exit_signal_series_by_symbol,
        params=config.params,
        execution_policy=asdict(config.execution),
        risk_policy=asdict(config.risk),
        config_hash=config.config_hash,
        data_source_by_symbol=data_source_by_symbol,
        backtest_revision=backtest_revision,
        replace_existing=replace_existing,
        bars_by_symbol=(
            {symbol: _input_bars(frame) for symbol, frame in symbol_frames.items()}
            if store_bars
            else None
        ),
    )
