"""Test the vectorised backtest engine in run_backtest_lumibot_btc.py.

Skills: python, quant

Validates:
  1. Backtest produces trades and equity snapshots on synthetic data
  2. Equity curve has correct number of snapshots (one per H1 bar after warmup)
  3. BacktestOutput builds correctly from trade/equity logs
  4. InfluxDB points include strategy_signals and perf_equity_curve with >1 row
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from scripts.run_backtest_lumibot_btc import (
    _build_backtest_output,
    _run_vectorised_backtest,
)
from scripts.etl.core_features import (
    add_daily_trend_gate,
    add_trendpullback_features,
    resample_ohlcv,
)
from quant_lab.monitoring.influx_writer import points_from_backtest


def _make_trending_btc_data(n_bars: int = 1200) -> pd.DataFrame:
    """Generate trending BTC data likely to trigger entry signals."""
    rng = np.random.default_rng(42)
    base = 80000.0
    # Create a trending market with occasional pullbacks
    returns = np.empty(n_bars)
    for i in range(n_bars):
        if i % 48 < 40:  # Trend up for 40 bars
            returns[i] = rng.normal(0.001, 0.005)
        else:  # Pullback for 8 bars
            returns[i] = rng.normal(-0.002, 0.005)

    close = base * np.cumprod(1 + returns)
    spread = rng.uniform(0.001, 0.004, size=n_bars)
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = np.empty(n_bars)
    open_[0] = base
    open_[1:] = close[:-1]
    volume = rng.uniform(500, 3000, size=n_bars)

    timestamps = pd.date_range("2025-07-01", periods=n_bars, freq="h", tz="UTC")
    return pd.DataFrame({
        "open": np.round(open_, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": np.round(close, 2),
        "volume": np.round(volume, 2),
    }, index=timestamps)


@pytest.fixture
def h1_d1():
    """Return (h1_with_features, d1_with_gate) for testing."""
    raw = _make_trending_btc_data()
    raw.index.name = "ts"
    h1 = add_trendpullback_features(raw)
    d1 = add_daily_trend_gate(resample_ohlcv(raw, "1D"))
    return h1, d1


class TestVectorisedBacktest:
    def test_produces_trades(self, h1_d1):
        h1, d1 = h1_d1
        trade_log, equity_log = _run_vectorised_backtest(h1, d1, budget=100_000)
        assert len(trade_log) > 0, "Should produce at least 1 trade on trending data"

    def test_equity_snapshots_match_bars(self, h1_d1):
        h1, d1 = h1_d1
        trade_log, equity_log = _run_vectorised_backtest(h1, d1, budget=100_000, warmup_days=7)
        # Equity snapshots should cover bars after warmup (one per bar from i=1 onward)
        warmup_ts = h1.index[0] + pd.Timedelta(days=7)
        # The loop iterates i=1..len-1 and emits an equity snapshot for each bar >= warmup_ts
        expected_min = (h1.index >= warmup_ts).sum() - 2
        expected_max = (h1.index >= warmup_ts).sum()
        assert expected_min <= len(equity_log) <= expected_max

    def test_trade_log_fields(self, h1_d1):
        h1, d1 = h1_d1
        trade_log, _ = _run_vectorised_backtest(h1, d1, budget=100_000)
        if trade_log:
            t = trade_log[0]
            assert "entry_ts" in t
            assert "exit_ts" in t
            assert "entry_price" in t
            assert "exit_price" in t
            assert "pnl" in t
            assert "bars_held" in t
            assert t["bars_held"] > 0

    def test_build_backtest_output(self, h1_d1):
        h1, d1 = h1_d1
        trade_log, equity_log = _run_vectorised_backtest(h1, d1, budget=100_000)
        output = _build_backtest_output(
            run_id="test-run-001",
            start_ts=datetime(2025, 7, 1, tzinfo=timezone.utc),
            end_ts=datetime(2025, 8, 20, tzinfo=timezone.utc),
            trade_log=trade_log,
            equity_log=equity_log,
            sample="test",
        )
        assert output.metrics.trades == len(trade_log)
        assert len(output.equity_curve) == len(equity_log)
        assert len(output.trades) == len(trade_log)

    def test_influx_points_complete(self, h1_d1):
        """InfluxDB points include strategy_signals and perf_equity_curve (>1 row)."""
        h1, d1 = h1_d1
        trade_log, equity_log = _run_vectorised_backtest(h1, d1, budget=100_000)
        output = _build_backtest_output(
            run_id="test-run-influx",
            start_ts=datetime(2025, 7, 1, tzinfo=timezone.utc),
            end_ts=datetime(2025, 8, 20, tzinfo=timezone.utc),
            trade_log=trade_log,
            equity_log=equity_log,
            sample="test",
        )
        points = points_from_backtest(output, sample="test", benchmark="BTC_BH")
        counts = Counter(p._name for p in points)

        assert counts["strategy_signals"] == len(trade_log) * 2  # entry + exit per trade
        assert counts["perf_equity_curve"] == len(equity_log)
        assert counts["perf_equity_curve"] > 1, "Equity curve should have >1 row"
        assert counts["strategy_performance"] == 1
        assert counts["trade_blotter"] == len(trade_log)

    def test_no_trades_on_flat_data(self):
        """Flat data should produce zero trades."""
        n = 500
        timestamps = pd.date_range("2025-07-01", periods=n, freq="h", tz="UTC")
        flat = pd.DataFrame({
            "open": [50000.0] * n,
            "high": [50001.0] * n,
            "low": [49999.0] * n,
            "close": [50000.0] * n,
            "volume": [100.0] * n,
        }, index=timestamps)
        flat.index.name = "ts"
        h1 = add_trendpullback_features(flat)
        d1 = add_daily_trend_gate(resample_ohlcv(flat, "1D"))
        trade_log, equity_log = _run_vectorised_backtest(h1, d1, budget=100_000)
        assert len(trade_log) == 0
