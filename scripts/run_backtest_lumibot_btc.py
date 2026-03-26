#!/usr/bin/env python3
"""Run TrendPullback backtest on real Binance BTC/USDT data (vectorised engine).

Skills: python, quant

Pipeline:
  1. Fetch BTC/USDT 1h OHLCV from Binance (paginated)
  2. Compute H1 features, resample H1→D1 for daily gate
  3. Vectorised bar-by-bar backtest with identical logic to Lumibot PoC
  4. Build BacktestOutput with canonical metrics (metrics.py)
  5. Write canonical measurements to InfluxDB

Usage:
    python scripts/run_backtest_lumibot_btc.py [--months 6] [--dry-run] [--no-influx]

Note: The original Lumibot engine has a known DST infinite-loop bug when
backtesting 24/7 crypto markets.  This script replaces it with a fast
vectorised loop that produces identical signals and is DST-safe.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_lab.backtest.adapter import generate_run_id
from quant_lab.backtest.archive import archive_backtest_parquet
from quant_lab.backtest.persistence import save_backtest_output
from quant_lab.backtest.schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
)
from quant_lab.config.market_config import (
    InstrumentConfig,
    calc_commission,
    calc_slippage,
    get_instrument,
)
from quant_lab.data.binance_fetcher import fetch_ohlcv
from quant_lab.monitoring.influx_writer import points_from_backtest, points_from_ohlcv
from quant_lab.signal_engine.trendpullback import (
    compute_daily_gate,
    compute_features,
    generate_signals,
    resample_to_daily,
)


# Load instrument config from markets.yaml
_INST = get_instrument("BTC_USDT")

STRATEGY_NAME = "trendpullback_lumibot"
SYMBOL = _INST.symbol
TIMEFRAME = "H1"
DATA_SOURCE = "binance_spot"

# Strategy parameters (from InstrumentConfig)
PULL = 0.3
MAX_HOLD_BARS = _INST.max_hold_bars
WARMUP_DAYS = _INST.warmup_bars // 24
INITIAL_BUDGET = 100_000.0


def _run_vectorised_backtest(
    h1: pd.DataFrame,
    d1: pd.DataFrame,
    budget: float = INITIAL_BUDGET,
    warmup_days: int = WARMUP_DAYS,
    commission_bps: float = _INST.commission_rate * 10_000,
    slippage_bps: float = 0.0,  # slippage now tick-based via InstrumentConfig
    inst: InstrumentConfig | None = None,
) -> tuple[list[dict], list[dict]]:
    """Bar-by-bar backtest over H1 data using signal_engine.

    Returns (trade_log, equity_log).
    Costs (commission + slippage) are deducted per round-trip from trade PnL.
    When inst is provided, uses InstrumentConfig for cost calculations.
    """
    if inst is None:
        inst = _INST
    # --- Merge daily gate into h1 (vectorised via merge_asof) ---
    h1_feat = h1.copy()
    h1_idx_name = h1_feat.index.name or "ts"
    h1_feat.index.name = h1_idx_name

    # Compute daily_trend bool on D1
    d1_trend = d1[["close", "ema20", "ema20_prev"]].copy()
    d1_trend["daily_trend"] = (
        (d1_trend["close"] > d1_trend["ema20"])
        & (d1_trend["ema20"] > d1_trend["ema20_prev"])
    )

    # Prepare D1 right-side DataFrame with explicit column name
    d1_right = d1_trend[["daily_trend"]].copy()
    d1_right["d1_ts"] = d1_right.index
    d1_right = d1_right.reset_index(drop=True)

    # merge_asof: for each H1 bar, find the most recent *completed* D1 bar
    # D1 index = bar open time, direction="backward" ensures no look-ahead
    h1_feat = pd.merge_asof(
        h1_feat.reset_index(),
        d1_right,
        left_on=h1_idx_name,
        right_on="d1_ts",
        direction="backward",
    ).set_index(h1_idx_name).drop(columns=["d1_ts"], errors="ignore")
    h1_feat["daily_trend"] = h1_feat["daily_trend"].fillna(False)

    # --- Generate signals via signal_engine ---
    sig_df = generate_signals(h1_feat)
    signals = sig_df["signal"].values

    # --- Execute trades following signals ---
    trade_log: list[dict] = []
    equity_log: list[dict] = []

    start_ts = h1.index[0] + pd.Timedelta(days=warmup_days)
    valid_mask = h1.index >= start_ts
    if valid_mask.sum() < 2:
        return trade_log, equity_log

    # Legacy bps fallback (kept for CLI override compatibility)
    cost_rate = (commission_bps + slippage_bps) / 10_000.0  # per side
    use_inst_cost = inst is not None

    cash = budget
    in_position = False
    entry_price = 0.0
    entry_ts: pd.Timestamp | None = None
    bars_held = 0
    qty = 0.0

    for i in range(len(h1)):
        t = h1.index[i]
        if t < start_ts:
            continue

        cur = h1.iloc[i]
        price = float(cur["close"])

        # Portfolio value
        pv = (cash + qty * price) if in_position else cash
        equity_log.append({"ts": t, "equity": float(pv)})

        sig = int(signals[i])

        if sig == -1 and in_position:
            # Exit
            exit_price = price
            gross_pnl = (exit_price - entry_price) * qty
            if use_inst_cost:
                entry_cost = calc_commission(inst, entry_price, qty) + calc_slippage(inst, qty)
                exit_cost = calc_commission(inst, exit_price, qty) + calc_slippage(inst, qty)
            else:
                entry_cost = entry_price * qty * cost_rate
                exit_cost = exit_price * qty * cost_rate
            total_cost = entry_cost + exit_cost
            net_pnl = gross_pnl - total_cost
            cash += qty * exit_price - exit_cost  # exit slippage+commission
            trade_log.append({
                "entry_ts": entry_ts,
                "exit_ts": t,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "side": "buy",
                "pnl": exit_price - entry_price,  # per-unit gross
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                "cost": total_cost,
                "bars_held": bars_held,
                "quantity": qty,
            })
            in_position = False
            entry_price = 0.0
            entry_ts = None
            bars_held = 0
            qty = 0.0

        elif sig == 1 and not in_position:
            # Entry — size qty so total outlay (price + cost) fits in available
            entry_price = price
            available = cash * 0.95
            if use_inst_cost:
                # Estimate cost per unit for sizing
                est_comm_rate = inst.commission_rate if inst.commission_rate > 0 else 0.0
                est_slip_per_unit = inst.slippage_ticks * inst.tick_value
                qty = available / (entry_price * (1 + est_comm_rate) + est_slip_per_unit)
            else:
                qty = available / (entry_price * (1 + cost_rate))
            if qty <= 0:
                continue
            if use_inst_cost:
                entry_cost = calc_commission(inst, entry_price, qty) + calc_slippage(inst, qty)
            else:
                entry_cost = entry_price * qty * cost_rate
            cash -= qty * entry_price + entry_cost
            in_position = True
            entry_ts = t
            bars_held = 0

        elif in_position:
            bars_held += 1

    # Force-close open position at last bar
    if in_position:
        last = h1.iloc[-1]
        exit_price = float(last["close"])
        gross_pnl = (exit_price - entry_price) * qty
        if use_inst_cost:
            entry_cost = calc_commission(inst, entry_price, qty) + calc_slippage(inst, qty)
            exit_cost = calc_commission(inst, exit_price, qty) + calc_slippage(inst, qty)
        else:
            entry_cost = entry_price * qty * cost_rate
            exit_cost = exit_price * qty * cost_rate
        total_cost = entry_cost + exit_cost
        net_pnl = gross_pnl - total_cost
        cash += qty * exit_price - exit_cost
        trade_log.append({
            "entry_ts": entry_ts,
            "exit_ts": h1.index[-1],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "side": "buy",
            "pnl": exit_price - entry_price,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "cost": total_cost,
            "bars_held": bars_held,
            "quantity": qty,
        })

    return trade_log, equity_log


def _build_backtest_output(
    run_id: str,
    start_ts: datetime,
    end_ts: datetime,
    trade_log: list[dict],
    equity_log: list[dict],
    sample: str | None = None,
    close_series: pd.Series | None = None,
) -> BacktestOutput:
    """Convert trade/equity logs to a BacktestOutput."""
    now = datetime.now(tz=timezone.utc)

    run_metadata = RunMetadata(
        run_id=run_id,
        strategy=STRATEGY_NAME,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        start_ts=start_ts,
        end_ts=end_ts,
        run_ts=now,
        data_source=DATA_SOURCE,
        data_version="1",
        sample=sample,
    )

    trade_records: list[TradeRecord] = []
    for i, td in enumerate(trade_log):
        entry_p = td["entry_price"]
        exit_p = td["exit_price"]
        pnl = td["pnl"]
        ets = td["entry_ts"]
        xts = td["exit_ts"]
        if isinstance(ets, pd.Timestamp):
            ets = ets.to_pydatetime()
        if isinstance(xts, pd.Timestamp):
            xts = xts.to_pydatetime()
        gross = td.get("gross_pnl", pnl)
        net = td.get("net_pnl", pnl)
        trade_records.append(TradeRecord(
            trade_id=f"{run_id}-t{i:04d}",
            entry_ts=ets,
            exit_ts=xts,
            symbol=SYMBOL,
            side=td["side"],
            entry_price=entry_p,
            exit_price=exit_p,
            quantity=td.get("quantity", 1.0),
            price_unit="USDT",
            quantity_unit=SYMBOL,
            gross_pnl=gross,
            net_pnl=net,
            pnl_unit="USDT",
            holding_bars=td.get("bars_held"),
        ))

    # Build Buy & Hold benchmark from close_series
    bh_equity_map: dict[pd.Timestamp, float] = {}
    bh_ret_map: dict[pd.Timestamp, float] = {}
    bh_total_return = 0.0
    if close_series is not None and len(close_series) > 0:
        first_close = float(close_series.iloc[0])
        if first_close > 0:
            bh_total_return = float(close_series.iloc[-1]) / first_close - 1.0
            bh_eq = close_series / first_close
            bh_ret = close_series.pct_change().fillna(0.0)
            for ts_idx in bh_eq.index:
                bh_equity_map[ts_idx] = float(bh_eq.loc[ts_idx])
                bh_ret_map[ts_idx] = float(bh_ret.loc[ts_idx])

    # Build equity curve from logged snapshots
    equity_points: list[EquityCurvePoint] = []
    if equity_log:
        initial_equity = equity_log[0]["equity"]
        peak = initial_equity
        for j, eq in enumerate(equity_log):
            eq_val = eq["equity"]
            ts = eq["ts"]
            if isinstance(ts, pd.Timestamp):
                ts_dt = ts.to_pydatetime()
            else:
                ts_dt = ts
            if eq_val > peak:
                peak = eq_val
            dd = (peak - eq_val) / peak if peak > 0 else 0.0
            prev_eq = equity_log[j - 1]["equity"] if j > 0 else eq_val
            ret_1d = (eq_val - prev_eq) / prev_eq if prev_eq > 0 else 0.0

            # Lookup BH benchmark for this timestamp
            bh_eq_val = bh_equity_map.get(eq["ts"])
            bh_ret_val = bh_ret_map.get(eq["ts"])

            equity_points.append(EquityCurvePoint(
                ts=ts_dt,
                equity=eq_val / initial_equity if initial_equity > 0 else 1.0,
                equity_unit="index",
                ret_1d=ret_1d,
                drawdown=dd,
                benchmark_equity=bh_eq_val,
                benchmark_ret_1d=bh_ret_val,
            ))

    # Compute aggregate metrics from trades
    n_trades = len(trade_records)
    if n_trades > 0:
        returns = [t.net_pnl / t.entry_price for t in trade_records]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]

        equity_vals = [1.0]
        for r in returns:
            equity_vals.append(equity_vals[-1] * (1 + r))

        total_ret = equity_vals[-1] / equity_vals[0] - 1.0
        peak_eq = equity_vals[0]
        max_dd = 0.0
        for v in equity_vals:
            if v > peak_eq:
                peak_eq = v
            dd_val = (peak_eq - v) / peak_eq
            if dd_val > max_dd:
                max_dd = dd_val

        avg_ret = float(np.mean(returns))
        std_ret = float(np.std(returns, ddof=1)) if len(returns) > 1 else 1.0
        gross_profit = sum(r for r in returns if r > 0)
        gross_loss = abs(sum(r for r in returns if r <= 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 0.0

        # Approximate annualization
        if trade_records[-1].exit_ts and trade_records[0].entry_ts:
            span = (trade_records[-1].exit_ts - trade_records[0].entry_ts).total_seconds()
            years = span / (365.25 * 86400) if span > 0 else 1.0
        else:
            years = 1.0
        ann_return = (1 + total_ret) ** (1 / years) - 1 if years > 0 else 0.0
        trades_per_year = n_trades / years if years > 0 else 0.0
        sharpe = (avg_ret / std_ret) * np.sqrt(trades_per_year) if std_ret > 0 else 0.0

        metrics = StrategyMetrics(
            total_return=total_ret,
            annual_return=float(ann_return),
            sharpe=float(sharpe),
            max_drawdown=max_dd,
            win_rate=len(wins) / n_trades,
            profit_factor=pf,
            avg_trade_return=avg_ret,
            avg_pnl_points=float(np.mean([t.net_pnl for t in trade_records])),
            trades=n_trades,
            bh_total_return=bh_total_return,
        )
    else:
        metrics = StrategyMetrics(total_return=0.0, trades=0, bh_total_return=bh_total_return)

    return BacktestOutput(
        run_metadata=run_metadata,
        equity_curve=equity_points,
        trades=trade_records,
        metrics=metrics,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest TrendPullback on real Binance BTC/USDT (vectorised)"
    )
    p.add_argument("--months", type=int, default=6, help="Lookback months for 1h data (default: 6)")
    p.add_argument("--out-dir", type=str, default=str(ROOT / "data" / "backtests"))
    p.add_argument("--no-influx", action="store_true", help="Skip InfluxDB write")
    p.add_argument("--dry-run", action="store_true", help="Print InfluxDB points without writing")
    p.add_argument("--sample", default="oos", help="Sample tag for InfluxDB (default: oos)")
    p.add_argument("--benchmark", default="BTC_BH", help="Benchmark tag (default: BTC_BH)")
    p.add_argument("--budget", type=float, default=100_000, help="Initial budget (default: 100000)")
    p.add_argument("--commission-bps", type=float, default=10.0, help="Commission in basis points per side (default: 10)")
    p.add_argument("--slippage-bps", type=float, default=5.0, help="Slippage in basis points per side (default: 5)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # 1) Fetch real H1 data
    print(f"[1/5] Fetching Binance {SYMBOL} 1h data ({args.months} months)...")
    h1_raw = fetch_ohlcv(symbol=SYMBOL, interval="1h", months=args.months)
    print(f"       bars={len(h1_raw)}, range={h1_raw['timestamp'].iloc[0]} ~ {h1_raw['timestamp'].iloc[-1]}")

    # Convert to indexed DataFrame
    h1_base = h1_raw.set_index("timestamp")
    h1_base.index.name = "ts"

    # 2) Compute features on H1; resample H1→D1 for daily gate (signal_engine)
    print("[2/5] Computing H1/D1 features (signal_engine + pandas_ta)...")
    h1 = compute_features(h1_base)
    d1 = compute_daily_gate(resample_to_daily(h1_base))
    print(f"       H1 bars={len(h1)}, D1 bars={len(d1)}")

    # 3) Run vectorised backtest
    print("[3/5] Running vectorised backtest...")
    run_id = generate_run_id(STRATEGY_NAME, SYMBOL)

    trade_log, equity_log = _run_vectorised_backtest(
        h1, d1, budget=args.budget, warmup_days=WARMUP_DAYS,
        commission_bps=args.commission_bps,
        slippage_bps=args.slippage_bps,
        inst=_INST,
    )
    print(f"       cost model: InstrumentConfig({_INST.symbol}) commission_rate={_INST.commission_rate}, slippage_ticks={_INST.slippage_ticks}")
    print(f"       trades={len(trade_log)}, equity_snapshots={len(equity_log)}")

    # 4) Build BacktestOutput
    print("[4/5] Building BacktestOutput...")
    data_start = h1_base.index[0]
    data_end = h1_base.index[-1]
    start_ts = data_start.to_pydatetime() if isinstance(data_start, pd.Timestamp) else data_start
    end_ts = data_end.to_pydatetime() if isinstance(data_end, pd.Timestamp) else data_end
    output = _build_backtest_output(
        run_id=run_id,
        start_ts=start_ts,
        end_ts=end_ts,
        trade_log=trade_log,
        equity_log=equity_log,
        sample=args.sample,
        close_series=h1_base["close"],
    )

    m = output.metrics
    print(f"       trades={m.trades}  sharpe={m.sharpe:.3f}  mdd={m.max_drawdown:.4f}  total_ret={m.total_return:.4f}")

    # Save JSON + Parquet archive
    out_dir = Path(args.out_dir)
    paths = save_backtest_output(output, out_dir)
    archive_paths = archive_backtest_parquet(output, ROOT / "data")
    print(f"       Saved: {paths['json']}")
    print(f"       Archive: {archive_paths['equity_curve']}, {archive_paths['trades']}")

    # 5) InfluxDB
    if args.no_influx:
        print("[5/5] InfluxDB write skipped (--no-influx)")
        return

    points = points_from_backtest(output, sample=args.sample, benchmark=args.benchmark)
    ohlcv_points = points_from_ohlcv(h1_base, symbol=SYMBOL, timeframe="1h", run_id=run_id, source="backtest")
    points.extend(ohlcv_points)
    counts = Counter(p._name for p in points)

    if args.dry_run:
        print(f"[5/5] [DRY-RUN] points={len(points)}, counts={dict(counts)}")
        return

    url = os.getenv("INFLUX_URL", "http://localhost:8086")
    org = os.getenv("INFLUX_ORG", "quant_research")
    bucket = os.getenv("INFLUX_BUCKET", "nautilus_signals")
    token = (
        os.getenv("INFLUX_TOKEN")
        or os.getenv("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN")
        or "change_me_super_secret_token"
    )

    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    with InfluxDBClient(url=url, token=token, org=org) as client:
        writer = client.write_api(write_options=SYNCHRONOUS)
        writer.write(bucket=bucket, org=org, record=points)

    print(f"[5/5] InfluxDB OK: points={len(points)}, counts={dict(counts)}, run_id={run_id}")


if __name__ == "__main__":
    main()
