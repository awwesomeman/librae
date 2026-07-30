"""Baseline BTC H1 regression test.

Pins deterministic SMA crossover backtest output on synthetic BTC H1 data
(seed=42, 720 bars). Any change to sample_data or the simple SMA crossover
logic will break these assertions, which is the intended behavior.
"""

from __future__ import annotations

import pytest
from librae.backtest.schema import RUN_ID_PATTERN
from tests.sample_data import (
    generate_btc_h1_ohlcv,
    run_simple_sma_crossover,
)

# -- Pinned baseline values (seed=42, fast=10, slow=30) --
EXPECTED_TRADES = 13
EXPECTED_WIN_RATE = pytest.approx(0.15384615384615385, abs=1e-9)
EXPECTED_MDD = pytest.approx(0.37907270052537334, abs=1e-9)
EXPECTED_EQUITY = pytest.approx(0.6209272994746267, abs=1e-9)
EXPECTED_AVG_PNL_POINTS = pytest.approx(-1943.2576923076917, abs=1e-4)
EXPECTED_PF = pytest.approx(0.07349755209319286, abs=1e-9)


@pytest.fixture
def btc_h1_data():
    return generate_btc_h1_ohlcv()


@pytest.fixture
def baseline_metrics(btc_h1_data):
    return run_simple_sma_crossover(btc_h1_data, fast_period=10, slow_period=30)


# ---------------------------------------------------------------------------
# Data reproducibility
# ---------------------------------------------------------------------------


class TestDataReproducibility:
    def test_shape(self, btc_h1_data):
        assert btc_h1_data.shape == (720, 6)

    def test_columns(self, btc_h1_data):
        assert list(btc_h1_data.columns) == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

    def test_first_close_deterministic(self, btc_h1_data):
        assert btc_h1_data["close"].iloc[0] == 60286.25
        assert btc_h1_data["open"].iloc[0] == 60_000.0

    def test_regeneration_identical(self):
        df1 = generate_btc_h1_ohlcv()
        df2 = generate_btc_h1_ohlcv()
        assert df1.equals(df2)


# ---------------------------------------------------------------------------
# Baseline regression
# ---------------------------------------------------------------------------


class TestBaselineRegression:
    def test_trade_count(self, baseline_metrics):
        assert baseline_metrics["trades"] == EXPECTED_TRADES

    def test_win_rate(self, baseline_metrics):
        assert baseline_metrics["win_rate"] == EXPECTED_WIN_RATE

    def test_max_drawdown(self, baseline_metrics):
        assert baseline_metrics["mdd"] == EXPECTED_MDD

    def test_equity(self, baseline_metrics):
        assert baseline_metrics["equity"] == EXPECTED_EQUITY

    def test_avg_pnl_points(self, baseline_metrics):
        assert baseline_metrics["avg_pnl_points"] == EXPECTED_AVG_PNL_POINTS

    def test_profit_factor(self, baseline_metrics):
        assert baseline_metrics["pf"] == EXPECTED_PF

    def test_trade_details_present(self, baseline_metrics):
        assert len(baseline_metrics["trade_details"]) == EXPECTED_TRADES


# ---------------------------------------------------------------------------
# run_id pattern contract
# ---------------------------------------------------------------------------


class TestRunIdContract:
    def test_run_id_pattern_valid(self):
        valid_ids = [
            "sma_crossover-btcusdt-h1-20240101t0000-abcd12",
            "mean_reversion-ethusdt-m5-20260325t1200-deadbe",
        ]
        for rid in valid_ids:
            assert RUN_ID_PATTERN.match(rid), f"Should match: {rid}"

    def test_run_id_pattern_invalid(self):
        invalid_ids = [
            "UpperCase-btcusdt-20240101t000000-abcd1234",
            "no-timestamp-here",
            "",
            "sma-btcusdt-2024-abcd1234",
            "sma_crossover-btcusdt-20240101t000000-abcd1234",
        ]
        for rid in invalid_ids:
            assert not RUN_ID_PATTERN.match(rid), f"Should not match: {rid}"
