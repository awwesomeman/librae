"""TimescaleDB writer — mirrors influx_writer but uses PostgreSQL.

Writes BacktestOutput to TimescaleDB tables:
  backtest_runs, equity_curve, trade_blotter,
  strategy_signals, strategy_performance, ohlcv
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras

from quant_lab.backtest.schema import BacktestOutput
from quant_lab.contracts import SCHEMA_VERSION

TIMESCALE_DSN = "postgresql://quant:quant_secret@localhost:5432/quant"


@contextmanager
def get_conn(dsn: str = TIMESCALE_DSN):
    """Yield a psycopg2 connection with auto-commit/rollback."""
    conn = psycopg2.connect(dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _to_dt(ts: Any) -> datetime | None:
    """Normalise timestamp to datetime."""
    if ts is None:
        return None
    if hasattr(ts, "to_pydatetime"):
        return ts.to_pydatetime()
    return ts


def write_backtest_output(output: BacktestOutput, dsn: str = TIMESCALE_DSN) -> dict:
    """Write a complete BacktestOutput to TimescaleDB. Returns counts dict."""
    meta = output.run_metadata
    m = output.metrics
    counts: dict[str, int] = {}

    with get_conn(dsn) as conn:
        cur = conn.cursor()

        # 1. backtest_runs (upsert)
        cur.execute(
            """INSERT INTO backtest_runs
               (run_id, strategy, symbol, timeframe, sample, data_source,
                start_ts, end_ts, run_ts, schema_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                 strategy=EXCLUDED.strategy, run_ts=EXCLUDED.run_ts""",
            (
                meta.run_id, meta.strategy, meta.symbol, meta.timeframe,
                meta.sample, meta.data_source,
                _to_dt(meta.start_ts), _to_dt(meta.end_ts),
                _to_dt(meta.run_ts), meta.schema_version or SCHEMA_VERSION,
            ),
        )
        counts["backtest_runs"] = 1

        # 2. equity_curve (batch)
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

        # 3. trade_blotter (batch)
        if output.trades:
            trade_rows = [
                (
                    tr.trade_id, meta.run_id,
                    _to_dt(tr.entry_ts), _to_dt(tr.exit_ts),
                    tr.symbol, tr.side,
                    tr.entry_price, tr.exit_price, tr.quantity,
                    tr.gross_pnl, tr.net_pnl,
                    tr.commission, tr.holding_bars,
                )
                for tr in output.trades
            ]
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO trade_blotter
                   (trade_id, run_id, entry_ts, exit_ts, symbol, side,
                    entry_price, exit_price, quantity,
                    gross_pnl, net_pnl, commission, holding_bars)
                   VALUES %s
                   ON CONFLICT (trade_id) DO NOTHING""",
                trade_rows,
                page_size=500,
            )
            counts["trade_blotter"] = len(trade_rows)

        # 4. strategy_signals (from trades: entry + exit)
        signal_rows = []
        for tr in output.trades:
            side = str(tr.side).lower()
            strength = 1.0 if side in {"buy", "long"} else -1.0
            # Entry signal
            signal_rows.append((
                _to_dt(tr.entry_ts), meta.run_id, meta.strategy,
                meta.symbol, meta.timeframe,
                "entry", meta.data_source,
                tr.entry_price, strength, 0.5, tr.quantity,
            ))
            # Exit signal
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

        # 5. strategy_performance (upsert)
        cur.execute(
            """INSERT INTO strategy_performance
               (run_id, total_return, annual_return, sharpe, sortino,
                max_drawdown, win_rate, profit_factor, trades,
                avg_trade_return, exposure_ratio, bh_total_return)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id) DO UPDATE SET
                 total_return=EXCLUDED.total_return,
                 sharpe=EXCLUDED.sharpe,
                 max_drawdown=EXCLUDED.max_drawdown""",
            (
                meta.run_id, m.total_return, m.annual_return,
                m.sharpe, getattr(m, "sortino", None),
                m.max_drawdown, m.win_rate, m.profit_factor,
                m.trades, m.avg_trade_return, m.exposure_ratio,
                m.bh_total_return,
            ),
        )
        counts["strategy_performance"] = 1

        cur.close()

    return counts
