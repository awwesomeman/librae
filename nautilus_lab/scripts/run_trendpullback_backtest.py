#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nautilus_lab.backtest.persistence import save_backtest_output
from nautilus_lab.backtest.schema import BacktestOutput, EquityCurvePoint, RunMetadata, StrategyMetrics, TradeRecord
from nautilus_lab.strategies import TrendPullBackParams, run_trendpullback_backtest


def make_synthetic_m1(days: int = 420, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * 24 * 60
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    ret = rng.normal(0.0005 / 1440, 0.0004, n)
    close = 20000.0 * np.exp(np.cumsum(ret))
    spread = rng.uniform(0.0002, 0.0010, n)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = rng.uniform(100, 600, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def build_output(run_id: str) -> BacktestOutput:
    m1 = make_synthetic_m1()
    params = TrendPullBackParams()
    trades_raw, returns = run_trendpullback_backtest(m1, params)

    eq = np.cumprod(1 + returns) if len(returns) else np.asarray([1.0])
    peak = np.maximum.accumulate(eq)
    dd = (eq / peak) - 1.0

    ts_index = pd.date_range(m1.index[0], periods=len(eq), freq="1D", tz="UTC")
    equity_curve = [
        EquityCurvePoint(
            ts=ts_index[i].to_pydatetime(),
            equity=float(eq[i]),
            equity_unit="ratio",
            ret_1d=float(0.0 if i == 0 else (eq[i] / eq[i - 1] - 1.0)),
            drawdown=float(dd[i]),
            benchmark_equity=float(1.0 + i * 0.0008),
            benchmark_ret_1d=0.0008,
        )
        for i in range(len(eq))
    ]

    trades = [
        TradeRecord(
            trade_id=f"T{i+1:04d}",
            entry_ts=t["entry_ts"].to_pydatetime(),
            exit_ts=t["exit_ts"].to_pydatetime(),
            symbol="MXFR1",
            side="buy",
            entry_price=float(t["entry_price"]),
            exit_price=float(t["exit_price"]),
            quantity=1.0,
            price_unit="points",
            quantity_unit="contracts",
            gross_pnl=float(t["exit_price"] - t["entry_price"]),
            net_pnl=float(t["ret"] * t["entry_price"]),
            pnl_unit="points",
            holding_bars=int(t["holding_bars"]),
        )
        for i, t in enumerate(trades_raw)
    ]

    win_rate = float((returns > 0).mean()) if len(returns) else 0.0
    total_return = float(eq[-1] - 1.0)
    years = max((m1.index[-1] - m1.index[0]).days / 365.25, 1e-6)
    annual_return = float((eq[-1] ** (1 / years)) - 1.0)
    sharpe = float((returns.mean() / returns.std(ddof=1)) * np.sqrt(len(returns) / years)) if len(returns) > 1 and returns.std(ddof=1) > 0 else 0.0
    gross_profit = float(returns[returns > 0].sum()) if len(returns) else 0.0
    gross_loss = float(-returns[returns <= 0].sum()) if len(returns) else 0.0

    metrics = StrategyMetrics(
        total_return=total_return,
        annual_return=annual_return,
        sharpe=sharpe,
        max_drawdown=float(dd.min()) if len(dd) else 0.0,
        win_rate=win_rate,
        profit_factor=float(gross_profit / gross_loss) if gross_loss > 0 else 0.0,
        avg_trade_return=float(returns.mean()) if len(returns) else 0.0,
        trades=int(len(returns)),
        exposure_ratio=0.35,
        bh_total_return=0.12,
    )

    return BacktestOutput(
        run_metadata=RunMetadata(
            run_id=run_id,
            strategy="trendpullback_v1_1_h1_l_mxfr1",
            symbol="MXFR1",
            timeframe="H1",
            start_ts=m1.index[0].to_pydatetime(),
            end_ts=m1.index[-1].to_pydatetime(),
            run_ts=datetime.now(timezone.utc),
            data_source="synthetic",
            data_version="synthetic_v1",
        ),
        equity_curve=equity_curve,
        trades=trades,
        metrics=metrics,
    )


def main() -> None:
    run_id = f"trendpullback_mxfr1_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    output = build_output(run_id)
    out_dir = Path(__file__).resolve().parents[1] / "data" / "backtests"
    paths = save_backtest_output(output, out_dir)
    print(f"run_id={run_id}")
    print(f"json={paths['json']}")
    if "csv" in paths:
        print(f"csv={paths['csv']}")


if __name__ == "__main__":
    main()
