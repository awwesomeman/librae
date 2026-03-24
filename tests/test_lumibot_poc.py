"""Tests for Lumibot PoC — signal concordance and module integrity."""
from __future__ import annotations

import unittest

import pandas as pd

from scripts.etl.core_features import (
    add_daily_trend_gate,
    add_trendpullback_features,
    resample_ohlcv,
)
from poc.lumibot.sample_data import generate_btc_1m
from poc.lumibot.signal_schema import signals_to_df
from poc.lumibot.reference_signals import extract_signals
from poc.lumibot.compare_signals import compare_signals, run_lumibot_signal_extraction
from poc.lumibot.strategy_lumibot import TrendPullbackLumibot


class TestSampleData(unittest.TestCase):
    def test_generate_btc_1m_shape(self):
        df = generate_btc_1m(start="2025-01-01", end="2025-01-10", seed=42)
        self.assertGreater(len(df), 1000)
        self.assertEqual(list(df.columns), ["open", "high", "low", "close", "volume"])

    def test_ohlc_consistency(self):
        df = generate_btc_1m(start="2025-01-01", end="2025-01-05", seed=42)
        self.assertTrue((df["high"] >= df["low"]).all())
        self.assertTrue((df["high"] >= df["close"]).all())
        self.assertTrue((df["high"] >= df["open"]).all())
        self.assertTrue((df["low"] <= df["close"]).all())
        self.assertTrue((df["low"] <= df["open"]).all())

    def test_resample_reduces_rows(self):
        m1 = generate_btc_1m(start="2025-01-01", end="2025-01-10", seed=42)
        h1 = resample_ohlcv(m1, "60min")
        self.assertLess(len(h1), len(m1))
        self.assertGreater(len(h1), 0)

    def test_deterministic(self):
        df1 = generate_btc_1m(start="2025-01-01", end="2025-01-05", seed=42)
        df2 = generate_btc_1m(start="2025-01-01", end="2025-01-05", seed=42)
        pd.testing.assert_frame_equal(df1, df2)


class TestReferenceSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m1 = generate_btc_1m(start="2025-01-01", end="2025-03-31", seed=42)
        cls.h1 = add_trendpullback_features(resample_ohlcv(cls.m1, "60min"))
        cls.d1 = add_daily_trend_gate(resample_ohlcv(cls.m1, "1D"))

    def test_features_columns(self):
        self.assertIn("ema20", self.h1.columns)
        self.assertIn("atr14", self.h1.columns)
        self.assertIn("vol_sma20", self.h1.columns)

    def test_daily_gate_columns(self):
        self.assertIn("ema20", self.d1.columns)
        self.assertIn("ema20_prev", self.d1.columns)

    def test_extract_signals_returns_list(self):
        signals = extract_signals(
            self.m1, self.h1, self.d1, "2025-01-15", "2025-03-25", run_id="test123"
        )
        self.assertIsInstance(signals, list)
        self.assertGreater(len(signals), 0)

    def test_signal_format(self):
        signals = extract_signals(
            self.m1, self.h1, self.d1, "2025-01-15", "2025-03-25", run_id="test123"
        )
        s = signals[0]
        self.assertEqual(s.side, "long")
        self.assertEqual(s.symbol, "BTCUSDT")
        self.assertIn("reference", s.strategy)
        self.assertEqual(s.run_id, "test123")
        self.assertIsInstance(s.timestamp, pd.Timestamp)

    def test_signals_to_df(self):
        signals = extract_signals(
            self.m1, self.h1, self.d1, "2025-01-15", "2025-03-25", run_id="test123"
        )
        df = signals_to_df(signals)
        self.assertEqual(
            list(df.columns), ["timestamp", "side", "symbol", "strategy", "run_id"]
        )
        self.assertEqual(len(df), len(signals))


class TestSignalConcordance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m1 = generate_btc_1m(start="2025-01-01", end="2025-03-31", seed=42)
        cls.h1 = add_trendpullback_features(resample_ohlcv(cls.m1, "60min"))
        cls.d1 = add_daily_trend_gate(resample_ohlcv(cls.m1, "1D"))
        cls.start = "2025-01-15"
        cls.end = "2025-03-25"
        cls.run_id = "concordance_test"
        # Pre-compute signals once for all concordance tests
        cls.ref_df = signals_to_df(
            extract_signals(cls.m1, cls.h1, cls.d1, cls.start, cls.end, run_id=cls.run_id)
        )
        cls.lumi_df = run_lumibot_signal_extraction(
            cls.m1, cls.h1, cls.d1, cls.start, cls.end, run_id=cls.run_id
        )
        cls.result = compare_signals(cls.ref_df, cls.lumi_df)

    def test_concordance_at_least_95_percent(self):
        self.assertGreaterEqual(
            self.result["concordance_rate"],
            0.95,
            f"Concordance {self.result['concordance_rate']:.2%} < 95%",
        )

    def test_exact_match(self):
        self.assertEqual(self.result["concordance_rate"], 1.0, "Expected 100% concordance")

    def test_empty_signals_concordance(self):
        empty = pd.DataFrame(
            columns=["timestamp", "side", "symbol", "strategy", "run_id"]
        )
        result = compare_signals(empty, empty)
        self.assertEqual(result["concordance_rate"], 1.0)


class TestLumibotStrategyClass(unittest.TestCase):
    def test_strategy_has_required_parameters(self):
        params = TrendPullbackLumibot.parameters
        self.assertIn("pull", params)
        self.assertIn("bn", params)
        self.assertIn("en", params)
        self.assertIn("run_id", params)

    def test_strategy_inherits_from_lumibot(self):
        from lumibot.strategies import Strategy
        self.assertTrue(issubclass(TrendPullbackLumibot, Strategy))


if __name__ == "__main__":
    unittest.main()
