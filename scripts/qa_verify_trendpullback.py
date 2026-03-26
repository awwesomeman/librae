#!/usr/bin/env python3
"""QA verification for TrendPullback strategy.

Checks:
  A. Look-ahead bias (truncation, daily gate alignment, entry bar isolation)
  B. Signal logic correctness (compare local signals vs InfluxDB)
  C. Per-trade PnL correctness
  D. Aggregate performance correctness
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_lab.signal_engine.trendpullback import (
    compute_daily_gate,
    compute_features,
    generate_signals,
    resample_to_daily,
)

# InfluxDB config
INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_ORG = os.getenv("INFLUX_ORG", "quant_research")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "nautilus_signals")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "change_me_super_secret_token")
RUN_ID = "trendpullback_lumibot-btcusdt-20260326t134948-4ef889e5"
INITIAL_BUDGET = 100_000.0
COST_RATE = (10 + 5) / 10_000  # 15 bps


def get_influx_client():
    from influxdb_client import InfluxDBClient
    return InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)


def query_influx(query: str) -> pd.DataFrame:
    with get_influx_client() as client:
        df = client.query_api().query_data_frame(query, org=INFLUX_ORG)
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
    return df


# ===========================================================================
# A. Look-ahead bias
# ===========================================================================

def prepare_h1_with_gate(raw: pd.DataFrame) -> pd.DataFrame:
    """Compute features + daily gate, return H1 with daily_trend column."""
    h1 = compute_features(raw)
    h1_idx_name = h1.index.name or "ts"
    h1.index.name = h1_idx_name

    d1 = compute_daily_gate(resample_to_daily(raw))
    d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
    d1_trend["daily_trend"] = (
        (d1_trend["close"] > d1_trend["ema20"])
        & (d1_trend["ema20"] > d1_trend["ema20_prev"])
    )
    d1_right = d1_trend[["daily_trend"]].copy()
    d1_right["d1_ts"] = d1_right.index
    d1_right = d1_right.reset_index(drop=True)

    h1 = pd.merge_asof(
        h1.reset_index(),
        d1_right,
        left_on=h1_idx_name,
        right_on="d1_ts",
        direction="backward",
    ).set_index(h1_idx_name).drop(columns=["d1_ts"], errors="ignore")
    h1["daily_trend"] = h1["daily_trend"].fillna(False)
    return h1


def check_a_lookahead():
    print("=== A. 前瞻偏誤 ===")

    # Load real data
    cache = ROOT / "data" / "cache" / "BTCUSDT_1h.parquet"
    raw = pd.read_parquet(cache)
    if "timestamp" in raw.columns:
        raw = raw.set_index("timestamp")
    raw.index.name = "ts"

    # A1. Truncation test
    N = len(raw) - 200
    h1_full = prepare_h1_with_gate(raw)
    sig_full = generate_signals(h1_full)["signal"].values

    raw_short = raw.iloc[:N].copy()
    h1_short = prepare_h1_with_gate(raw_short)
    sig_short = generate_signals(h1_short)["signal"].values

    overlap = len(sig_short)
    trunc_match = np.array_equal(sig_full[:overlap], sig_short)
    status = "PASS" if trunc_match else "FAIL"
    print(f"[{status}] 截斷測試：前 {N} 根訊號{'一致' if trunc_match else '不一致'}")
    if not trunc_match:
        diffs = np.where(sig_full[:overlap] != sig_short)[0]
        print(f"       差異 bar indices: {diffs[:10]}")

    # A2. Daily gate alignment
    d1 = compute_daily_gate(resample_to_daily(raw))
    d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
    d1_trend["daily_trend"] = (
        (d1_trend["close"] > d1_trend["ema20"])
        & (d1_trend["ema20"] > d1_trend["ema20_prev"])
    )
    d1_right = d1_trend[["daily_trend"]].copy()
    d1_right["d1_ts"] = d1_right.index
    d1_right = d1_right.reset_index(drop=True)

    h1_feat = compute_features(raw)
    h1_feat.index.name = "ts"
    merged = pd.merge_asof(
        h1_feat.reset_index(),
        d1_right,
        left_on="ts",
        right_on="d1_ts",
        direction="backward",
    ).set_index("ts")

    valid = merged["d1_ts"].dropna()
    gate_ok = (valid <= valid.index).all()
    status = "PASS" if gate_ok else "FAIL"
    print(f"[{status}] Daily gate：merge_asof 方向正確（D1 ts <= H1 ts）")

    # Show a specific example
    sample_bar = merged[merged["d1_ts"].notna()].iloc[50]
    bar_ts = sample_bar.name
    d1_ts = sample_bar["d1_ts"]
    print(f"       範例：H1 bar {bar_ts} 用了 D1 bar {d1_ts}")

    # A3. Entry bar isolation (mutate bar i+1)
    entries = np.where(sig_full == 1)[0]
    isolation_ok = True
    tested = 0
    for idx in entries[:5]:
        if idx + 1 >= len(h1_full):
            continue
        h1_mut = h1_full.copy()
        h1_mut.iloc[idx + 1, h1_mut.columns.get_loc("close")] = 1.0
        h1_mut.iloc[idx + 1, h1_mut.columns.get_loc("high")] = 1.0
        h1_mut.iloc[idx + 1, h1_mut.columns.get_loc("low")] = 0.5
        h1_mut.iloc[idx + 1, h1_mut.columns.get_loc("volume")] = 0.0
        sig_mut = generate_signals(h1_mut)
        if sig_mut["signal"].iloc[idx] != 1:
            isolation_ok = False
            print(f"       [FAIL] Entry bar {idx} changed after mutating bar {idx+1}")
        tested += 1

    status = "PASS" if isolation_ok else "FAIL"
    print(f"[{status}] Entry bar 隔離：測試 {tested} 筆 entry，bar i+1 突變不影響 bar i 訊號")
    print()


# ===========================================================================
# B. Signal comparison with InfluxDB
# ===========================================================================

def check_b_signals():
    print("=== B. 訊號對比 ===")

    # Local signals
    cache = ROOT / "data" / "cache" / "BTCUSDT_1h.parquet"
    raw = pd.read_parquet(cache)
    if "timestamp" in raw.columns:
        raw = raw.set_index("timestamp")
    raw.index.name = "ts"

    h1 = prepare_h1_with_gate(raw)
    sig = generate_signals(h1)

    local_entries = sig[sig["signal"] == 1].index.tolist()
    local_exits = sig[sig["signal"] == -1].index.tolist()

    # Query InfluxDB for strategy_signals
    q = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -1y)
      |> filter(fn: (r) => r._measurement == "strategy_signals")
      |> filter(fn: (r) => r.run_id == "{RUN_ID}")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    df_influx = query_influx(q)

    if df_influx.empty:
        print("[WARN] InfluxDB strategy_signals 無資料，嘗試從 trade_blotter 推導")
        # Fallback: use trade_blotter to get entry/exit timestamps
        q2 = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -1y)
          |> filter(fn: (r) => r._measurement == "trade_blotter")
          |> filter(fn: (r) => r.run_id == "{RUN_ID}")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        df_trades = query_influx(q2)
        if df_trades.empty:
            print("[SKIP] trade_blotter 也無資料，跳過訊號對比")
            print()
            return [], []

        # Extract entry/exit from trade_blotter
        influx_entries = []
        influx_exits = []
        if "entry_ts" in df_trades.columns:
            influx_entries = pd.to_datetime(df_trades["entry_ts"], utc=True).tolist()
        if "exit_ts" in df_trades.columns:
            influx_exits = pd.to_datetime(df_trades["exit_ts"], utc=True).tolist()

        # Match local signals to trade_blotter
        # Build trade pairs from local signals
        local_trades = []
        i_exit = 0
        for ent in local_entries:
            # Find the next exit after this entry
            while i_exit < len(local_exits) and local_exits[i_exit] <= ent:
                i_exit += 1
            if i_exit < len(local_exits):
                local_trades.append((ent, local_exits[i_exit]))
                i_exit += 1

        print(f"本地訊號：{len(local_entries)} entries, {len(local_exits)} exits → {len(local_trades)} trades")
        print(f"InfluxDB：{len(influx_entries)} entries, {len(influx_exits)} exits")

        # Compare
        match_count = 0
        mismatch_count = 0
        for idx, (le, lx) in enumerate(local_trades):
            if idx < len(influx_entries):
                ie = influx_entries[idx]
                ix = influx_exits[idx] if idx < len(influx_exits) else None
                le_tz = le if le.tzinfo else le.tz_localize("UTC")
                lx_tz = lx if lx.tzinfo else lx.tz_localize("UTC")

                entry_match = abs((le_tz - ie).total_seconds()) < 3600
                exit_match = ix is not None and abs((lx_tz - ix).total_seconds()) < 3600

                e_sym = "✅" if entry_match else "❌"
                x_sym = "✅" if exit_match else "❌"
                print(f"T{idx+1}: entry {le} → {e_sym}  exit {lx} → {x_sym}")
                if entry_match and exit_match:
                    match_count += 1
                else:
                    mismatch_count += 1
                    if not entry_match:
                        print(f"       local={le_tz}, influx={ie}")
                    if not exit_match:
                        print(f"       local={lx_tz}, influx={ix}")
            else:
                print(f"T{idx+1}: entry {le} exit {lx} → InfluxDB 無對應 ⚠️")
                mismatch_count += 1

        status = "PASS" if mismatch_count == 0 else f"WARN ({mismatch_count} mismatches)"
        print(f"[{status}] 訊號比對：{match_count}/{len(local_trades)} match")
        print()
        return local_trades, influx_entries
    else:
        # Parse signals from InfluxDB
        if "signal" in df_influx.columns:
            influx_entries_df = df_influx[df_influx["signal"] == 1]
            influx_exits_df = df_influx[df_influx["signal"] == -1]
            print(f"本地：{len(local_entries)} entries, {len(local_exits)} exits")
            print(f"InfluxDB：{len(influx_entries_df)} entries, {len(influx_exits_df)} exits")

        print(f"[INFO] InfluxDB strategy_signals 有 {len(df_influx)} 筆")
        print()
        return local_entries, local_exits


# ===========================================================================
# C. Per-trade PnL verification
# ===========================================================================

def check_c_trade_pnl():
    print("=== C. 每筆績效 ===")

    q = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -1y)
      |> filter(fn: (r) => r._measurement == "trade_blotter")
      |> filter(fn: (r) => r.run_id == "{RUN_ID}")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    df = query_influx(q)

    if df.empty:
        print("[SKIP] trade_blotter 無資料")
        print()
        return pd.DataFrame()

    print(f"InfluxDB trade_blotter: {len(df)} trades")
    print(f"Columns: {list(df.columns)}")

    all_ok = True
    results = []

    for idx, row in df.iterrows():
        entry_p = float(row.get("entry_price", 0))
        exit_p = float(row.get("exit_price", 0))
        qty = float(row.get("quantity", 0))
        net_pnl_influx = float(row.get("net_pnl", 0))
        gross_pnl_influx = float(row.get("gross_pnl", 0))

        # Manual calculation
        gross_pnl_calc = (exit_p - entry_p) * qty
        entry_cost = entry_p * qty * COST_RATE
        exit_cost = exit_p * qty * COST_RATE
        net_pnl_calc = gross_pnl_calc - entry_cost - exit_cost

        diff = abs(net_pnl_influx - net_pnl_calc)
        ok = diff < 0.01
        sym = "✅" if ok else "❌"
        if not ok:
            all_ok = False

        trade_id = row.get("trade_id", f"T{idx}")
        print(f"{trade_id}: entry={entry_p:.1f} exit={exit_p:.1f} qty={qty:.3f} "
              f"net_pnl={net_pnl_influx:.2f} calc={net_pnl_calc:.2f} diff={diff:.4f} → {sym}")

        results.append({
            "trade_id": trade_id,
            "entry_price": entry_p,
            "exit_price": exit_p,
            "quantity": qty,
            "net_pnl_influx": net_pnl_influx,
            "net_pnl_calc": net_pnl_calc,
            "diff": diff,
            "ok": ok,
        })

    status = "PASS" if all_ok else "FAIL"
    print(f"[{status}] 每筆績效驗算：{sum(r['ok'] for r in results)}/{len(results)} match")
    print()
    return pd.DataFrame(results)


# ===========================================================================
# D. Aggregate performance verification
# ===========================================================================

def check_d_aggregate(trade_df: pd.DataFrame):
    print("=== D. 整段績效 ===")

    # Query InfluxDB strategy_performance
    q = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -1y)
      |> filter(fn: (r) => r._measurement == "strategy_performance")
      |> filter(fn: (r) => r.run_id == "{RUN_ID}")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    df_perf = query_influx(q)

    if df_perf.empty:
        print("[WARN] strategy_performance 無資料，嘗試從 trade_blotter 自行計算")

    # Calculate from trade_blotter
    q2 = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -1y)
      |> filter(fn: (r) => r._measurement == "trade_blotter")
      |> filter(fn: (r) => r.run_id == "{RUN_ID}")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    df_trades = query_influx(q2)

    if df_trades.empty:
        print("[SKIP] 無交易資料可驗算")
        print()
        return

    net_pnls = df_trades["net_pnl"].astype(float).values
    entry_prices = df_trades["entry_price"].astype(float).values
    n_trades = len(net_pnls)

    # The backtest uses compounded per-unit returns: net_pnl / entry_price
    # total_return = product(1 + r_i) - 1
    per_unit_returns = net_pnls / entry_prices
    equity_vals = [1.0]
    for r in per_unit_returns:
        equity_vals.append(equity_vals[-1] * (1 + r))
    total_return_calc = equity_vals[-1] / equity_vals[0] - 1.0

    win_rate_calc = float(np.sum(net_pnls > 0)) / n_trades if n_trades > 0 else 0.0

    # Profit factor uses per-unit returns (same as backtest)
    wins_r = [r for r in per_unit_returns if r > 0]
    losses_r = [r for r in per_unit_returns if r <= 0]
    gross_profit = sum(wins_r)
    gross_loss = abs(sum(losses_r))
    profit_factor_calc = gross_profit / gross_loss if gross_loss > 0 else 0.0

    # Max drawdown from compounded equity vals
    mdd_calc = 0.0
    peak_eq = equity_vals[0]
    for v in equity_vals:
        if v > peak_eq:
            peak_eq = v
        dd = (peak_eq - v) / peak_eq if peak_eq > 0 else 0.0
        if dd > mdd_calc:
            mdd_calc = dd

    # Compare with InfluxDB strategy_performance
    if not df_perf.empty:
        influx_tr = float(df_perf.iloc[0].get("total_return", 0))
        influx_wr = float(df_perf.iloc[0].get("win_rate", 0))
        influx_mdd = float(df_perf.iloc[0].get("max_drawdown", 0))
        influx_pf = float(df_perf.iloc[0].get("profit_factor", 0))

        checks = [
            ("total_return", influx_tr, total_return_calc),
            ("win_rate", influx_wr, win_rate_calc),
            ("max_drawdown", influx_mdd, mdd_calc),
            ("profit_factor", influx_pf, profit_factor_calc),
        ]

        for name, influx_val, calc_val in checks:
            diff = abs(influx_val - calc_val)
            ok = diff < 0.001
            sym = "✅" if ok else "❌"
            print(f"{name}: InfluxDB={influx_val:.4f} calculated={calc_val:.4f} diff={diff:.6f} → {sym}")
    else:
        # Just print calculated values
        print(f"total_return: calculated={total_return_calc:.4f} (sum(net_pnl)/budget)")
        print(f"win_rate: calculated={win_rate_calc:.4f} ({int(np.sum(net_pnls > 0))}/{n_trades})")
        print(f"profit_factor: calculated={profit_factor_calc:.4f}")
        if mdd_calc > 0:
            print(f"max_drawdown: calculated={mdd_calc:.4f}")
        else:
            print("max_drawdown: equity curve 無資料，無法驗算")

        print("[INFO] strategy_performance 無資料，僅顯示手算值供人工對比")

    print()


# ===========================================================================
# Main
# ===========================================================================

def main():
    print("=" * 60)
    print(f"QA Verification: TrendPullback")
    print(f"run_id: {RUN_ID}")
    print("=" * 60)
    print()

    check_a_lookahead()
    check_b_signals()
    trade_df = check_c_trade_pnl()
    check_d_aggregate(trade_df)

    print("=" * 60)
    print("QA 驗證完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
