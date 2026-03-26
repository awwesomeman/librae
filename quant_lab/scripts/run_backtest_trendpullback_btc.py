#!/usr/bin/env python3
"""Backtest runner for TrendPullBack on Binance BTC spot H1.

Usage:
    python -m quant_lab.scripts.run_backtest_trendpullback_btc [--save-dir DIR]

Uses deterministic sample data by default.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from quant_lab.backtest import (
    compute_all,
    metrics_dict_to_backtest_output,
    save_backtest_output,
)
from quant_lab.strategies.trendpullback_btc import TrendPullBackParams, backtest
from quant_lab.scripts._shared_data import prepare_sample_data

SYMBOL = "BTCUSDT"
STRATEGY = "trendpullback"
TIMEFRAME = "H1"
COST_BPS = 8


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest TrendPullBack BTC")
    parser.add_argument("--save-dir", default="output/backtest", help="Directory to save results")
    args = parser.parse_args()

    m1, h1, d1 = prepare_sample_data()
    params = TrendPullBackParams(
        pullback_atr_ratio=0.30,
        breakout_lookback_bars=5,
        ema_span=20,
        time_stop_bars=6,
        round_trip_cost_bps=COST_BPS,
    )

    start_str = str(h1.index[0].date())
    end_str = str(h1.index[-1].date())

    metrics = backtest(m1, h1, d1, params, start=start_str, end=end_str)
    print(f"Trades: {metrics['trades']}, Return: {metrics.get('equity', 1.0):.4f}, "
          f"Sharpe: {metrics.get('ann_sharpe', 0):.3f}, MDD: {metrics.get('mdd', 0):.3%}")

    output = metrics_dict_to_backtest_output(
        metrics,
        strategy=STRATEGY,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        start=start_str,
        end=end_str,
        data_source="synthetic_sample",
        sample="oos",
    )

    computed = compute_all(output)
    print("\nComputed metrics:")
    for name, result in sorted(computed.items()):
        print(f"  {name}: {result.value:.6f} ({result.unit})")

    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    paths = save_backtest_output(output, str(save_dir))
    print(f"\nSaved: {paths}")


if __name__ == "__main__":
    main()
