#!/usr/bin/env python3
"""Run the TrendPullback strategy through Lumibot's full backtest engine.

Usage:
    python -m poc.lumibot.run_lumibot_backtest
"""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime

from lumibot.backtesting import PandasDataBacktesting
from lumibot.entities import Asset, Data

from scripts.etl.core_features import (
    add_daily_trend_gate,
    add_trendpullback_features,
    resample_ohlcv,
)
from poc.lumibot.sample_data import generate_btc_1m
from poc.lumibot.strategy_lumibot import TrendPullbackLumibot


def run_full_backtest() -> dict:
    """Run the TrendPullback strategy through Lumibot's full backtest engine."""
    print("Generating synthetic data...")
    m1 = generate_btc_1m(start="2025-01-01", end="2025-03-31", seed=42)
    h1 = add_trendpullback_features(resample_ohlcv(m1, "60min"))
    d1 = add_daily_trend_gate(resample_ohlcv(m1, "1D"))

    run_id = uuid.uuid4().hex[:12]

    btc_asset = Asset("BTC", asset_type="crypto")
    usd_asset = Asset("USD", asset_type="forex")

    # Avoid DST edge cases in Lumibot's US-Eastern tz_localize
    bt_start = datetime(2025, 1, 15)
    bt_end = datetime(2025, 3, 1)

    lumi_df = m1.copy()
    if lumi_df.index.tz is not None:
        lumi_df.index = lumi_df.index.tz_localize(None)

    pandas_data = Data(
        btc_asset,
        lumi_df,
        date_start=bt_start,
        date_end=bt_end,
        timestep="minute",
        quote=usd_asset,
        timezone="UTC",
    )

    print("Running Lumibot backtest engine...")
    with tempfile.TemporaryDirectory() as tmpdir:
        results = TrendPullbackLumibot.backtest(
            PandasDataBacktesting,
            backtesting_start=bt_start,
            backtesting_end=bt_end,
            pandas_data=[pandas_data],
            parameters={
                "pull": 0.3, "bn": 5, "en": 20, "run_id": run_id,
                "_h1": h1, "_d1": d1, "_m1": m1,
            },
            budget=100000,
            show_plot=False,
            show_tearsheet=False,
            save_tearsheet=False,
            save_stats_file=False,
            save_logfile=False,
            show_progress_bar=True,
            quiet_logs=True,
            stats_file=os.path.join(tmpdir, "stats.csv"),
            plot_file_html=os.path.join(tmpdir, "plot.html"),
            name="TrendPullback_Lumibot_PoC",
        )

    print("\nLumibot backtest completed.")
    if results:
        for key in ["total_return", "sharpe", "max_drawdown"]:
            if key in results:
                print(f"  {key}: {results[key]}")

    return results


if __name__ == "__main__":
    run_full_backtest()
