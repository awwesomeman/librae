"""E2E smoke test: Binance data -> backtest -> InfluxDB points.

Skills: python, quant

Validates the full pipeline without hitting real Binance or InfluxDB:
  1. Mock-fetch OHLCV data
  2. Run SMA crossover backtest
  3. Build BacktestOutput via adapter
  4. Generate InfluxDB points via influx_writer
  5. Verify point measurements match canonical schema
"""
from __future__ import annotations

import unittest.mock as mock
from collections import Counter
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from nautilus_lab.backtest.adapter import generate_run_id, metrics_dict_to_backtest_output
from nautilus_lab.backtest.persistence import save_backtest_output, load_backtest_output
from nautilus_lab.backtest.sample_data import run_simple_sma_crossover
from nautilus_lab.backtest.schema import RUN_ID_PATTERN
from nautilus_lab.monitoring.influx_writer import points_from_backtest


def _make_realistic_btc_df(n_bars: int = 720) -> pd.DataFrame:
    """Generate realistic-ish BTC OHLCV data for testing."""
    rng = np.random.default_rng(123)
    base = 60000.0
    returns = rng.normal(0.0002, 0.012, size=n_bars)
    close = base * np.cumprod(1 + returns)
    spread = rng.uniform(0.002, 0.006, size=n_bars)
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = np.empty(n_bars)
    open_[0] = base
    open_[1:] = close[:-1]
    volume = rng.uniform(100, 2000, size=n_bars)

    timestamps = pd.date_range("2024-07-01", periods=n_bars, freq="h", tz="UTC")
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": np.round(open_, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": np.round(close, 2),
        "volume": np.round(volume, 2),
    })


@pytest.fixture
def btc_df():
    return _make_realistic_btc_df()


class TestE2EPipeline:
    def test_backtest_on_mock_binance_data(self, btc_df):
        """Full pipeline: data -> backtest -> BacktestOutput -> InfluxDB points."""
        # 1) Backtest
        metrics = run_simple_sma_crossover(btc_df, fast_period=10, slow_period=30)
        assert metrics["trades"] > 0

        # 2) Adapter -> BacktestOutput
        start_str = btc_df["timestamp"].iloc[0].isoformat()
        end_str = btc_df["timestamp"].iloc[-1].isoformat()

        run_id = generate_run_id("sma_crossover", "BTCUSDT")
        output = metrics_dict_to_backtest_output(
            metrics,
            strategy="sma_crossover",
            symbol="BTCUSDT",
            timeframe="H1",
            start=start_str,
            end=end_str,
            data_source="binance_spot",
            run_id=run_id,
            sample="oos",
        )
        assert RUN_ID_PATTERN.match(output.run_metadata.run_id)
        assert output.run_metadata.data_source == "binance_spot"
        assert output.metrics.trades == metrics["trades"]

        # 3) InfluxDB points
        points = points_from_backtest(output, sample="oos", benchmark="BTC_BH")
        counts = Counter(p._name for p in points)

        assert counts["strategy_performance"] == 1
        assert counts["trade_blotter"] == metrics["trades"]
        assert counts["trade_distribution"] == 1
        assert counts["strategy_signals"] == metrics["trades"]

    def test_json_roundtrip(self, btc_df, tmp_path):
        """BacktestOutput can be saved and reloaded."""
        metrics = run_simple_sma_crossover(btc_df, fast_period=10, slow_period=30)
        run_id = generate_run_id("sma_crossover", "BTCUSDT")
        output = metrics_dict_to_backtest_output(
            metrics,
            strategy="sma_crossover",
            symbol="BTCUSDT",
            timeframe="H1",
            start=btc_df["timestamp"].iloc[0].isoformat(),
            end=btc_df["timestamp"].iloc[-1].isoformat(),
            data_source="binance_spot",
            run_id=run_id,
            sample="oos",
        )
        paths = save_backtest_output(output, tmp_path)
        loaded = load_backtest_output(paths["json"])

        assert loaded.run_metadata.run_id == run_id
        assert loaded.run_metadata.data_source == "binance_spot"
        assert loaded.metrics.trades == metrics["trades"]
        assert len(loaded.trades) == metrics["trades"]

    def test_influx_points_have_required_tags(self, btc_df):
        """Each InfluxDB point has strategy, symbol, run_id tags."""
        metrics = run_simple_sma_crossover(btc_df, fast_period=10, slow_period=30)
        run_id = generate_run_id("sma_crossover", "BTCUSDT")
        output = metrics_dict_to_backtest_output(
            metrics,
            strategy="sma_crossover",
            symbol="BTCUSDT",
            timeframe="H1",
            start=btc_df["timestamp"].iloc[0].isoformat(),
            end=btc_df["timestamp"].iloc[-1].isoformat(),
            data_source="binance_spot",
            run_id=run_id,
            sample="oos",
        )
        points = points_from_backtest(output, sample="oos", benchmark="BTC_BH")
        for pt in points:
            lp = pt.to_line_protocol()
            assert "strategy=" in lp
            assert "symbol=" in lp
            assert "run_id=" in lp
