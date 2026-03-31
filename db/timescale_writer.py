"""TimescaleDB writer for BacktestOutput and live signals.

Writes to tables: backtest_runs, equity_curve, trade_blotter,
strategy_signals, strategy_performance, ohlcv.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import psycopg2
import psycopg2.extras

import pandas as pd

from librae.backtest.schema import BacktestOutput
from librae.backtest.schema import SCHEMA_VERSION
from db import TIMESCALE_DSN, get_pool

logger = logging.getLogger(__name__)


@contextmanager
def get_conn(dsn: str = TIMESCALE_DSN):
    """Yield a psycopg2 connection from the pool with auto-commit/rollback."""
    pool = get_pool(dsn)
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


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
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single run record to backtest_runs (upsert)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO backtest_runs
               (run_id, strategy, symbol, timeframe, sample, data_source,
                start_ts, end_ts, run_ts, schema_version, mode, poll_interval)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                 strategy=EXCLUDED.strategy, run_ts=EXCLUDED.run_ts,
                 mode=EXCLUDED.mode, poll_interval=EXCLUDED.poll_interval""",
            (
                run_id, strategy, symbol, timeframe, sample, data_source,
                _to_dt(start_ts), _to_dt(end_ts),
                _to_dt(run_ts) or datetime.now(tz=timezone.utc),
                SCHEMA_VERSION, mode, poll_interval,
            ),
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
    dsn: str = TIMESCALE_DSN,
) -> dict:
    """Write a complete BacktestOutput to TimescaleDB.

    Args:
        output: BacktestOutput to write.
        dsn: TimescaleDB DSN.

    Returns:
        Dict mapping table names to row counts written.
    """
    meta = output.run_metadata
    m = output.metrics
    counts: dict[str, int] = {}

    write_run_metadata(
        run_id=meta.run_id, strategy=meta.strategy, symbol=meta.symbol,
        timeframe=meta.timeframe, mode=getattr(meta, "mode", None),
        start_ts=meta.start_ts, end_ts=meta.end_ts, run_ts=meta.run_ts,
        data_source=getattr(meta, "data_source", None), sample=getattr(meta, "sample", None), dsn=dsn,
    )
    counts["backtest_runs"] = 1

    with get_conn(dsn) as conn:
        cur = conn.cursor()

        # 清除舊資料（idempotent re-run）
        cur.execute("DELETE FROM strategy_signals WHERE run_id = %s", (meta.run_id,))
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
                )
                for eq in output.equity_curve
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO equity_curve
                   (ts, run_id, equity, benchmark_equity, drawdown, ret_1d, benchmark_ret_1d)
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
                    tr.commission, tr.slippage, tr.holding_bars,
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
                    commission, slippage, holding_bars)
                   VALUES %s
                   ON CONFLICT (trade_id) DO NOTHING""",
                trade_rows,
                page_size=500,
            )
            counts["trade_blotter"] = len(trade_rows)

        # strategy_signals (derived from trades: entry + exit)
        signal_rows = []
        for tr in output.trades:
            side = str(tr.side).lower()
            strength = 1.0 if side in {"buy", "long"} else -1.0
            signal_rows.append((
                _to_dt(tr.entry_ts), meta.run_id, meta.strategy,
                meta.symbol, meta.timeframe,
                "entry", getattr(meta, "data_source", None),
                tr.entry_price, strength, 0.5, tr.quantity,
            ))
            signal_rows.append((
                _to_dt(tr.exit_ts), meta.run_id, meta.strategy,
                meta.symbol, meta.timeframe,
                "exit", getattr(meta, "data_source", None),
                tr.exit_price, -strength, 0.5, tr.quantity,
            ))
        if signal_rows:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO strategy_signals
                   (ts, run_id, strategy, symbol, timeframe,
                    signal_type, source, price, signal_strength,
                    confidence, quantity)
                   VALUES %s""",
                signal_rows,
                page_size=1000,
            )
            counts["strategy_signals"] = len(signal_rows)

        cur.close()

    write_performance(meta.run_id, m, dsn=dsn)
    counts["strategy_performance"] = 1

    return counts


def write_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    run_id: str,
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
               ON CONFLICT (ts, symbol, timeframe, run_id) DO NOTHING""",
            rows,
            page_size=2000,
        )
        cur.close()

    return len(rows)


def write_signal(
    ts: datetime,
    run_id: str,
    strategy: str,
    symbol: str,
    timeframe: str,
    signal_type: str,
    source: str,
    price: float,
    signal_strength: float = 1.0,
    confidence: float = 0.5,
    quantity: float = 0.0,
    dsn: str = TIMESCALE_DSN,
) -> bool:
    """Write a single signal row to strategy_signals.

    Uses ON CONFLICT DO NOTHING for idempotent re-inserts (e.g. sim restart).
    Returns True if a row was inserted, False if it was a duplicate.
    """
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO strategy_signals
               (ts, run_id, strategy, symbol, timeframe,
                signal_type, source, price, signal_strength,
                confidence, quantity)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ts, run_id, symbol, signal_type) DO NOTHING""",
            (
                _to_dt(ts), run_id, strategy, symbol, timeframe,
                signal_type, source, price, signal_strength,
                confidence, quantity,
            ),
        )
        inserted = cur.rowcount > 0
        cur.close()
    return inserted


def write_equity_point(
    ts: datetime,
    run_id: str,
    equity: float,
    drawdown: float = 0.0,
    ret_1d: float = 0.0,
    benchmark_equity: float | None = None,
    benchmark_ret_1d: float | None = None,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write a single equity curve point (upsert by ts + run_id)."""
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO equity_curve
               (ts, run_id, equity, benchmark_equity, drawdown, ret_1d, benchmark_ret_1d)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, ts) DO UPDATE SET
                 equity=EXCLUDED.equity, drawdown=EXCLUDED.drawdown,
                 ret_1d=EXCLUDED.ret_1d""",
            (
                _to_dt(ts), run_id, equity, benchmark_equity,
                drawdown, ret_1d, benchmark_ret_1d,
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
                commission, slippage, holding_bars)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (trade_id) DO NOTHING""",
            (
                trade_id, run_id, _to_dt(entry_ts), _to_dt(exit_ts),
                symbol, side, entry_price, exit_price, quantity,
                gross_pnl, net_pnl, gross_return, net_return,
                commission, slippage, holding_bars,
            ),
        )
        cur.close()


def write_performance(
    run_id: str,
    metrics: Any,
    dsn: str = TIMESCALE_DSN,
) -> None:
    """Write/update strategy_performance for a run_id.

    Args:
        run_id: Run identifier.
        metrics: StrategyMetrics dataclass (or any object with matching attributes).
    """
    with get_conn(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO strategy_performance
               (run_id, total_return, annual_return, sharpe, sortino, calmar,
                max_drawdown, win_rate, profit_factor, trades,
                avg_trade_return, exposure_ratio, benchmark_return,
                total_commission, total_slippage)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                 total_slippage=EXCLUDED.total_slippage""",
            (
                run_id, metrics.total_return, metrics.annual_return,
                metrics.sharpe, metrics.sortino, metrics.calmar,
                metrics.max_drawdown, metrics.win_rate, metrics.profit_factor,
                metrics.trades, metrics.avg_trade_return, metrics.exposure_ratio,
                metrics.benchmark_return, metrics.total_commission, metrics.total_slippage,
            ),
        )
        cur.close()


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
    equity_values = [float(row["equity"]) for row in eq_df.to_dict("records")]
    timestamps = [row["_time"] for row in eq_df.to_dict("records")]
    trade_pnls = [
        _NS(
            gross_pnl=r.get("gross_pnl", 0), net_pnl=r.get("net_pnl", 0),
            commission=r.get("commission", 0), slippage=r.get("slippage", 0),
            tax=0, gross_return=0.0, net_return=0.0,
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
