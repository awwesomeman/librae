#!/usr/bin/env python3
"""Tests for librae.adapter — legacy dict → BacktestOutput conversion."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from librae.backtest.schema import BacktestOutput, StrategyMetrics, RunMetadata
from librae.utils import metrics_dict_to_backtest_output
from librae.core.utils import generate_run_id as _generate_run_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_METRICS = {
    "trades": 42,
    "win_rate": 0.55,
    "avg_ret": 0.003,
    "pf": 1.8,
    "equity": 1.15,
    "ann_return": 0.12,
    "ann_sharpe": 1.5,
    "ann_vol": 0.08,
    "mdd": 0.07,
}

ZERO_TRADE_METRICS = {"trades": 0}

CONTEXT = {
    "strategy": "TrendPullback_v1.0.0-H1-L-MXFR1",
    "symbol": "MXFR1",
    "timeframe": "H1",
    "start": "2024-01-01",
    "end": "2025-06-30",
    "data_source": "Shioaji",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMetricsDictToBacktestOutput(unittest.TestCase):
    """metrics_dict_to_backtest_output produces valid BacktestOutput."""

    def setUp(self):
        self.output = metrics_dict_to_backtest_output(
            SAMPLE_METRICS, **CONTEXT
        )

    def test_returns_backtest_output(self):
        self.assertIsInstance(self.output, BacktestOutput)

    def test_validate_passes(self):
        self.output.validate()

    def test_run_metadata_fields(self):
        meta = self.output.run_metadata
        self.assertEqual(meta.strategy, CONTEXT["strategy"])
        self.assertEqual(meta.symbol, CONTEXT["symbol"])
        self.assertEqual(meta.timeframe, CONTEXT["timeframe"])
        self.assertIsNotNone(meta.run_id)
        self.assertIsNotNone(meta.run_ts)

    def test_start_end_tz_aware(self):
        meta = self.output.run_metadata
        self.assertIsNotNone(meta.start_ts.tzinfo)
        self.assertIsNotNone(meta.end_ts.tzinfo)

    def test_metrics_mapping(self):
        m = self.output.metrics
        self.assertEqual(m.trades, 42)
        self.assertAlmostEqual(m.annual_return, 0.12)
        self.assertAlmostEqual(m.sharpe, 1.5)
        self.assertAlmostEqual(m.max_drawdown, 0.07)
        self.assertAlmostEqual(m.win_rate, 0.55)
        self.assertAlmostEqual(m.profit_factor, 1.8)
        self.assertAlmostEqual(m.avg_trade_return, 0.003)
        self.assertAlmostEqual(m.total_return, 0.15)

    def test_empty_equity_curve_and_trades(self):
        self.assertEqual(len(self.output.equity_curve), 0)
        self.assertEqual(len(self.output.trades), 0)

    def test_custom_run_id(self):
        output = metrics_dict_to_backtest_output(
            SAMPLE_METRICS, run_id="my-custom-id", **CONTEXT
        )
        self.assertEqual(output.run_metadata.run_id, "my-custom-id")

    def test_sample_label(self):
        output = metrics_dict_to_backtest_output(
            SAMPLE_METRICS, sample="oos", **CONTEXT
        )
        self.assertEqual(output.run_metadata.sample, "oos")


class TestZeroTradeConversion(unittest.TestCase):
    """Zero-trade edge case: legacy returns only {"trades": 0}."""

    def setUp(self):
        self.output = metrics_dict_to_backtest_output(
            ZERO_TRADE_METRICS, **CONTEXT
        )

    def test_returns_backtest_output(self):
        self.assertIsInstance(self.output, BacktestOutput)

    def test_validate_passes(self):
        self.output.validate()

    def test_zero_metrics(self):
        m = self.output.metrics
        self.assertEqual(m.trades, 0)
        self.assertAlmostEqual(m.total_return, 0.0)
        self.assertAlmostEqual(m.sharpe, 0.0)
        self.assertAlmostEqual(m.max_drawdown, 0.0)


class TestNoneMetricValues(unittest.TestCase):
    """Legacy may return None for sharpe/pf when insufficient data."""

    def test_none_values_default_to_zero(self):
        metrics = {
            "trades": 3,
            "win_rate": 0.33,
            "avg_ret": -0.01,
            "pf": None,
            "equity": 0.97,
            "ann_return": -0.05,
            "ann_sharpe": None,
            "ann_vol": None,
            "mdd": 0.03,
        }
        output = metrics_dict_to_backtest_output(metrics, **CONTEXT)
        output.validate()
        self.assertAlmostEqual(output.metrics.sharpe, 0.0)
        self.assertAlmostEqual(output.metrics.profit_factor, 0.0)


class TestGenerateRunId(unittest.TestCase):
    """run_id generation produces expected format."""

    def test_run_id_format(self):
        rid = _generate_run_id("MyStrategy", "MXFR1")
        self.assertTrue(rid.startswith("mystrategy-mxfr1-"))
        parts = rid.split("-")
        self.assertGreaterEqual(len(parts), 4)

    def test_run_ids_unique(self):
        ids = {_generate_run_id("s", "x") for _ in range(100)}
        self.assertEqual(len(ids), 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
