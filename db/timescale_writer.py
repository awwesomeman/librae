"""TimescaleDB writer for BacktestOutput.

Writes to tables: backtest_runs, equity_curve, trade_blotter,
strategy_signals, strategy_performance.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg2
import psycopg2.extras

import pandas as pd

from librae.schema import BacktestOutput
from librae.contracts import SCHEMA_VERSION
from db import TIMESCALE_DSN, get_pool


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

    with get_conn(dsn) as conn:
        cur = conn.cursor()

        # backtest_runs (upsert)
        cur.execute(
            """INSERT INTO backtest_runs
               (run_id, strategy, symbol, timeframe, sample, data_source,
                start_ts, end_ts, run_ts, schema_version, mode)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                 strategy=EXCLUDED.strategy, run_ts=EXCLUDED.run_ts,
                 mode=EXCLUDED.mode""",
            (
                meta.run_id, meta.strategy, meta.symbol, meta.timeframe,
                meta.sample, meta.data_source,
                _to_dt(meta.start_ts), _to_dt(meta.end_ts),
                _to_dt(meta.run_ts), meta.schema_version or SCHEMA_VERSION,
                meta.mode,
            ),
        )
        counts["backtest_runs"] = 1

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
                "entry", meta.data_source,
                tr.entry_price, strength, 0.5, tr.quantity,
            ))
            signal_rows.append((
                _to_dt(tr.exit_ts), meta.run_id, meta.strategy,
                meta.symbol, meta.timeframe,
                "exit", meta.data_source,
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

        # strategy_performance (upsert — full column coverage)
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
                meta.run_id, m.total_return, m.annual_return,
                m.sharpe, m.sortino, m.calmar,
                m.max_drawdown, m.win_rate, m.profit_factor,
                m.trades, m.avg_trade_return, m.exposure_ratio,
                m.benchmark_return, m.total_commission, m.total_slippage,
            ),
        )
        counts["strategy_performance"] = 1

        cur.close()

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
