#!/usr/bin/env python3
"""Seed comprehensive demo data for Strategy Dashboard.

Populates all tables: backtest_runs, equity_curve, trade_events,
strategy_performance, ohlcv. Covers multiple position
lifecycles with scaling, partial close, wins, losses, long and short.

Usage:
    source .env.local
    python scripts/seed_order_events_demo.py

Cleanup:
    python scripts/seed_order_events_demo.py --clean
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import psycopg2
from psycopg2.extras import execute_values

from librae.core.utils import make_event_id, make_trade_id

DSN = os.environ.get("TIMESCALE_DSN", "postgresql://quant:quant_secret@localhost:5432/quant")
RUN_ID = "demo_full-btcusdt-h1-20260310t0000-seed01"
SYMBOL = "BTCUSDT"
TIMEFRAME = "H1"
N_BARS = 200  # ~8 days of H1
START = datetime(2026, 3, 10, 0, 0, tzinfo=timezone.utc)
INITIAL_BALANCE = 100_000.0


def _ts(bar: int) -> datetime:
    return START + timedelta(hours=bar)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def ensure_trade_events_table(cur):
    """Recreate trade_events table matching current schema (independent, no FK)."""
    cur.execute("DROP TABLE IF EXISTS trade_events CASCADE;")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_events (
            event_id        TEXT NOT NULL,
            run_id          TEXT,
            strategy        TEXT NOT NULL,
            mode            TEXT NOT NULL,
            timeframe       TEXT NOT NULL,
            ts              TIMESTAMPTZ NOT NULL,
            symbol          TEXT,
            side            TEXT,
            event_type      TEXT,
            quantity        DOUBLE PRECISION,
            price           DOUBLE PRECISION,
            entry_price     DOUBLE PRECISION,
            position_quantity DOUBLE PRECISION,
            notional        DOUBLE PRECISION,
            commission      DOUBLE PRECISION DEFAULT 0,
            slippage        DOUBLE PRECISION DEFAULT 0,
            tax             DOUBLE PRECISION DEFAULT 0,
            pnl             DOUBLE PRECISION,
            net_return      DOUBLE PRECISION,
            entry_ts        TIMESTAMPTZ,
            holding_periods    INTEGER,
            reason          TEXT,
            CONSTRAINT chk_event_side CHECK (side IN ('long', 'short')),
            CONSTRAINT chk_event_type CHECK (
                event_type IN ('open', 'add', 'reduce', 'close')
            ),
            CONSTRAINT chk_event_mode CHECK (mode IN ('backtest', 'sim', 'live'))
        );
    """)
    cur.execute("SELECT create_hypertable('trade_events', 'ts', if_not_exists => TRUE);")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_events_pk ON trade_events(event_id, ts);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_run_id ON trade_events(run_id, ts DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_strategy ON trade_events(strategy, mode, symbol, ts DESC);")


def clean(cur):
    for tbl in ("trade_events", "equity_curve",
                "strategy_performance", "backtest_runs"):
        cur.execute(f"DELETE FROM {tbl} WHERE run_id = %s", (RUN_ID,))
    # ohlcv has no run_id — clean by symbol + time range + data_source
    cur.execute(
        "DELETE FROM ohlcv WHERE symbol = %s AND data_source = 'seed' AND ts >= %s AND ts < %s",
        (SYMBOL, START, _ts(N_BARS)),
    )
    print(f"Cleaned run_id={RUN_ID}")


# ---------------------------------------------------------------------------
# OHLCV generation
# ---------------------------------------------------------------------------

def generate_ohlcv() -> list[dict]:
    rng = np.random.default_rng(99)
    returns = rng.normal(0.0003, 0.012, size=N_BARS)
    base = 65_000.0
    close = base * np.cumprod(1 + returns)
    noise = rng.uniform(0.002, 0.006, size=N_BARS)
    high = close * (1 + noise)
    low = close * (1 - noise)
    opn = np.empty(N_BARS)
    opn[0] = base
    opn[1:] = close[:-1]
    vol = rng.uniform(200, 3000, size=N_BARS)
    return [
        {"ts": _ts(i), "open": float(round(opn[i], 2)), "high": float(round(high[i], 2)),
         "low": float(round(low[i], 2)), "close": float(round(close[i], 2)),
         "volume": float(round(vol[i], 2))}
        for i in range(N_BARS)
    ]


# ---------------------------------------------------------------------------
# Trade scenarios
# ---------------------------------------------------------------------------

def build_scenarios(ohlcv: list[dict]):
    """Build trade_events rows from scripted scenarios.

    Returns (events, trades) — trades used only for perf calculation, not DB insert.
    """
    def px(bar: int) -> float:
        return ohlcv[bar]["close"]

    events = []
    trades = []
    eidx = 0
    tidx = 0

    def add_event(bar, symbol, side, etype, qty, price, avg_entry, pos_qty,
                  realized=None, net_ret=None, entry_bar=None, hold=None, reason=""):
        nonlocal eidx
        events.append({
            "event_id": make_event_id(RUN_ID, eidx), "ts": _ts(bar), "symbol": symbol,
            "side": side, "event_type": etype, "quantity": qty, "price": price,
            "entry_price": avg_entry, "position_quantity": pos_qty,
            "notional": round(price * qty, 2),
            "commission": round(price * qty * 0.001, 2),
            "slippage": round(qty * 0.5, 2),
            "tax": 0.0,
            "pnl": realized, "net_return": net_ret,
            "entry_ts": _ts(entry_bar) if entry_bar is not None else None,
            "holding_periods": hold, "reason": reason,
        })
        eidx += 1

    def add_trade(entry_bar, exit_bar, symbol, side, entry_px, exit_px, qty,
                  gross_pnl, net_pnl, gross_ret, net_ret, comm, slip, tax, hold):
        nonlocal tidx
        trades.append({
            "trade_id": make_trade_id(RUN_ID, tidx),
            "entry_ts": _ts(entry_bar), "exit_ts": _ts(exit_bar),
            "symbol": symbol, "side": side,
            "entry_price": entry_px, "exit_price": exit_px, "quantity": qty,
            "gross_pnl": gross_pnl, "net_pnl": net_pnl,
            "gross_return": gross_ret, "net_return": net_ret,
            "commission": comm, "slippage": slip, "tax": tax,
            "holding_periods": hold,
        })
        tidx += 1

    # === Lifecycle 1: BTC Long — open, add, reduce, close (WIN) ===
    p1 = px(20)   # ~65200
    p2 = px(35)   # add price
    avg1 = round((p1 * 2 + p2 * 1) / 3, 2)
    p3 = px(60)   # reduce price (higher = win)
    p4 = px(80)   # close price

    add_event(20, SYMBOL, "long", "open", 2.0, p1, p1, 2.0, reason="SMA golden cross")
    add_event(35, SYMBOL, "long", "add", 1.0, p2, avg1, 3.0, reason="pullback buy")

    rpnl1 = round((p3 - avg1) * 2, 2)
    rret1 = round((p3 - avg1) / avg1 * 100, 2)
    comm1 = round(p3 * 2 * 0.001 + avg1 * 2 * 0.001, 2)
    add_event(60, SYMBOL, "long", "reduce", 2.0, p3, avg1, 1.0,
              realized=rpnl1, net_ret=rret1, entry_bar=20, hold=40, reason="take profit 66%")
    add_trade(20, 60, SYMBOL, "long", avg1, p3, 2.0,
              round((p3 - avg1) * 2, 2), rpnl1, rret1, rret1, comm1, 1.0, 0.0, 40)

    rpnl2 = round((p4 - avg1) * 1, 2)
    rret2 = round((p4 - avg1) / avg1 * 100, 2)
    comm2 = round(p4 * 1 * 0.001 + avg1 * 1 * 0.001, 2)
    add_event(80, SYMBOL, "long", "close", 1.0, p4, avg1, 0.0,
              realized=rpnl2, net_ret=rret2, entry_bar=20, hold=60, reason="trend reversal")
    add_trade(20, 80, SYMBOL, "long", avg1, p4, 1.0,
              round((p4 - avg1) * 1, 2), rpnl2, rret2, rret2, comm2, 0.5, 0.0, 60)

    # === Lifecycle 2: BTC Short — open, close (LOSS) ===
    p5 = px(90)
    p6 = px(110)  # price went up = loss for short

    add_event(90, SYMBOL, "short", "open", 1.5, p5, p5, 1.5, reason="bearish divergence")

    rpnl3 = round((p5 - p6) * 1.5, 2)
    rret3 = round((p5 - p6) / p5 * 100, 2)
    comm3 = round((p5 + p6) * 1.5 * 0.001, 2)
    add_event(110, SYMBOL, "short", "close", 1.5, p6, p5, 0.0,
              realized=rpnl3, net_ret=rret3, entry_bar=90, hold=20, reason="stop loss")
    add_trade(90, 110, SYMBOL, "short", p5, p6, 1.5,
              round((p5 - p6) * 1.5, 2), rpnl3, rret3, rret3, comm3, 0.75, 0.0, 20)

    # === Lifecycle 3: BTC Long — simple open/close (WIN) ===
    p7 = px(130)
    p8 = px(170)

    add_event(130, SYMBOL, "long", "open", 1.0, p7, p7, 1.0, reason="breakout")

    rpnl4 = round((p8 - p7) * 1.0, 2)
    rret4 = round((p8 - p7) / p7 * 100, 2)
    comm4 = round((p7 + p8) * 1.0 * 0.001, 2)
    add_event(170, SYMBOL, "long", "close", 1.0, p8, p7, 0.0,
              realized=rpnl4, net_ret=rret4, entry_bar=130, hold=40, reason="target hit")
    add_trade(130, 170, SYMBOL, "long", p7, p8, 1.0,
              round((p8 - p7) * 1.0, 2), rpnl4, rret4, rret4, comm4, 0.5, 0.0, 40)

    # === Lifecycle 4: BTC Short — open, add, reduce, close (WIN) ===
    p9 = px(175)
    p10 = px(180)
    avg2 = round((p9 * 1.0 + p10 * 0.5) / 1.5, 2)
    p11 = px(190)  # lower = win for short
    p12 = px(198)

    add_event(175, SYMBOL, "short", "open", 1.0, p9, p9, 1.0, reason="evening star")
    add_event(180, SYMBOL, "short", "add", 0.5, p10, avg2, 1.5, reason="confirmation")

    rpnl5 = round((avg2 - p11) * 0.8, 2)
    rret5 = round((avg2 - p11) / avg2 * 100, 2)
    comm5 = round((avg2 + p11) * 0.8 * 0.001, 2)
    add_event(190, SYMBOL, "short", "reduce", 0.8, p11, avg2, 0.7,
              realized=rpnl5, net_ret=rret5, entry_bar=175, hold=15, reason="cover partial")
    add_trade(175, 190, SYMBOL, "short", avg2, p11, 0.8,
              round((avg2 - p11) * 0.8, 2), rpnl5, rret5, rret5, comm5, 0.4, 0.0, 15)

    rpnl6 = round((avg2 - p12) * 0.7, 2)
    rret6 = round((avg2 - p12) / avg2 * 100, 2)
    comm6 = round((avg2 + p12) * 0.7 * 0.001, 2)
    add_event(198, SYMBOL, "short", "close", 0.7, p12, avg2, 0.0,
              realized=rpnl6, net_ret=rret6, entry_bar=175, hold=23, reason="target hit")
    add_trade(175, 198, SYMBOL, "short", avg2, p12, 0.7,
              round((avg2 - p12) * 0.7, 2), rpnl6, rret6, rret6, comm6, 0.35, 0.0, 23)

    return events, trades


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------

def build_equity_curve(ohlcv, events):
    """Build per-bar equity from events — simplified simulation."""
    cash = INITIAL_BALANCE
    pos_qty = 0.0
    pos_side = None
    avg_entry = 0.0
    curve = []
    peak = INITIAL_BALANCE
    prev_eq = INITIAL_BALANCE

    # Index events by bar
    event_map: dict[int, list[dict]] = {}
    for e in events:
        bar = int((e["ts"] - START).total_seconds() / 3600)
        event_map.setdefault(bar, []).append(e)

    for i in range(N_BARS):
        close = ohlcv[i]["close"]

        # Process events at this bar
        for ev in event_map.get(i, []):
            et = ev["event_type"]
            if et in ("open", "add"):
                cost = ev["price"] * ev["quantity"] + ev["commission"] + ev["slippage"]
                cash -= cost
                if et == "open":
                    pos_qty = ev["quantity"]
                    pos_side = ev["side"]
                    avg_entry = ev["price"]
                else:
                    avg_entry = ev["entry_price"]
                    pos_qty = ev["position_quantity"]
            elif et in ("reduce", "close"):
                proceeds = ev["pnl"] + ev["price"] * ev["quantity"] - ev["commission"] - ev["slippage"]
                cash += proceeds
                pos_qty = ev["position_quantity"]
                if pos_qty == 0:
                    pos_side = None

        # Mark-to-market
        if pos_qty > 0 and pos_side:
            if pos_side == "long":
                unrealized = (close - avg_entry) * pos_qty
            else:
                unrealized = (avg_entry - close) * pos_qty
            equity = cash + avg_entry * pos_qty + unrealized
        else:
            equity = cash

        peak = max(peak, equity)
        dd = (equity - peak) / peak if peak > 0 else 0.0
        ret_1d = (equity - prev_eq) / prev_eq if prev_eq > 0 else 0.0
        curve.append((_ts(i), equity, dd, ret_1d))
        prev_eq = equity

    return curve


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------

def compute_performance(trades, equity_curve):
    net_pnls = [t["net_pnl"] for t in trades]
    wins = [p for p in net_pnls if p > 0]
    losses = [abs(p) for p in net_pnls if p < 0]
    total_ret = (equity_curve[-1][1] - INITIAL_BALANCE) / INITIAL_BALANCE
    max_dd = min(e[2] for e in equity_curve)
    return {
        "total_return": round(total_ret, 6),
        "annual_return": None,
        "sharpe": None,
        "sortino": None,
        "calmar": None,
        "max_drawdown": round(max_dd, 6),
        "win_rate": round(len(wins) / len(net_pnls), 4) if net_pnls else 0,
        "profit_factor": round(sum(wins) / (sum(losses) + 1e-10), 4) if losses else None,
        "trades": len(trades),
        "avg_trade_return": round(float(np.mean([t["net_return"] for t in trades])) / 100, 6) if trades else 0,
        "exposure_ratio": None,
        "benchmark_return": None,
        "total_commission": round(sum(t["commission"] for t in trades), 2),
        "total_slippage": round(sum(t["slippage"] for t in trades), 2),
        "total_tax": 0.0,
    }


# ---------------------------------------------------------------------------
# DB writers
# ---------------------------------------------------------------------------

def seed(cur):
    ohlcv = generate_ohlcv()
    events, trades = build_scenarios(ohlcv)
    equity_curve = build_equity_curve(ohlcv, events)
    perf = compute_performance(trades, equity_curve)

    # backtest_runs
    cur.execute(
        "INSERT INTO backtest_runs (run_id, strategy, symbol, timeframe, mode,"
        " start_ts, end_ts, run_ts, data_source)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING",
        (RUN_ID, "demo_full", SYMBOL, TIMEFRAME, "backtest",
         _ts(0), _ts(N_BARS - 1), datetime.now(timezone.utc), "seed"),
    )

    # ohlcv
    execute_values(
        cur,
        "INSERT INTO ohlcv (ts, symbol, timeframe, data_source, open, high, low, close, volume)"
        " VALUES %s ON CONFLICT (ts, symbol, timeframe, data_source) DO NOTHING",
        [(o["ts"], SYMBOL, TIMEFRAME, "seed",
          o["open"], o["high"], o["low"], o["close"], o["volume"]) for o in ohlcv],
    )

    # trade_events
    execute_values(
        cur,
        "INSERT INTO trade_events"
        " (event_id, run_id, strategy, mode, timeframe,"
        "  ts, symbol, side, event_type,"
        "  quantity, price, entry_price, position_quantity, notional,"
        "  commission, slippage, tax, pnl, net_return,"
        "  entry_ts, holding_periods, reason)"
        " VALUES %s ON CONFLICT (event_id, ts) DO NOTHING",
        [(e["event_id"], RUN_ID, "demo_full", "backtest", TIMEFRAME,
          e["ts"], e["symbol"], e["side"], e["event_type"],
          e["quantity"], e["price"], e["entry_price"], e["position_quantity"],
          e["notional"], e["commission"], e["slippage"], e["tax"],
          e["pnl"], e["net_return"], e["entry_ts"], e["holding_periods"],
          e["reason"]) for e in events],
    )

    # equity_curve
    execute_values(
        cur,
        "INSERT INTO equity_curve (ts, run_id, equity, benchmark_equity, drawdown, ret_1d)"
        " VALUES %s ON CONFLICT (run_id, ts) DO NOTHING",
        [(ts, RUN_ID, eq, None, dd, ret) for ts, eq, dd, ret in equity_curve],
    )

    # strategy_performance
    cur.execute(
        "INSERT INTO strategy_performance"
        " (run_id, total_return, annual_return, sharpe, sortino, calmar,"
        "  max_drawdown, win_rate, profit_factor, trades, avg_trade_return,"
        "  exposure_ratio, benchmark_return,"
        "  total_commission, total_slippage, total_tax)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (run_id) DO NOTHING",
        (RUN_ID, perf["total_return"], perf["annual_return"],
         perf["sharpe"], perf["sortino"], perf["calmar"],
         perf["max_drawdown"], perf["win_rate"], perf["profit_factor"],
         perf["trades"], perf["avg_trade_return"], perf["exposure_ratio"],
         perf["benchmark_return"],
         perf["total_commission"], perf["total_slippage"], perf["total_tax"]),
    )

    print(f"Seeded run_id={RUN_ID}")
    print(f"  {len(ohlcv)} ohlcv bars ({SYMBOL} {TIMEFRAME})")
    print(f"  {len(events)} trade_events (4 lifecycles)")
    print(f"  {len(trades)} close/reduce events (for perf calc)")
    print(f"  {len(equity_curve)} equity_curve points")
    print(f"  Performance: return={perf['total_return']:.4%}, "
          f"MDD={perf['max_drawdown']:.4%}, "
          f"win_rate={perf['win_rate']:.0%}, "
          f"PF={perf['profit_factor']}")


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            ensure_trade_events_table(cur)
            if "--clean" in sys.argv:
                clean(cur)
            else:
                clean(cur)
                seed(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
