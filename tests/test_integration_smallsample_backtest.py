#!/usr/bin/env python3
"""
Small-sample integration backtest tests.

Tests logic/performance/lookahead issues using synthetic deterministic data.
No network, no external files.

Test A — sane metric bounds on a short window.
Test B — anti-lookahead sentinel: future d1 mutations must not change results.
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.etl.core_features import (
    add_daily_trend_gate,
    add_trendpullback_features,
    resample_ohlcv,
)
from scripts.strategies.strategy_trendpullback_v1_1_0_h1_l_btc import run_backtest


# ---------------------------------------------------------------------------
# Synthetic 1-minute OHLCV generator (deterministic)
# ---------------------------------------------------------------------------

def make_synthetic_m1(days: int = 30, seed: int = 42) -> pd.DataFrame:
    """Return a deterministic synthetic 1-minute OHLCV DataFrame for *days* days.

    Price follows a log-normal random walk with a slight upward drift so that
    the daily trend gate fires and the strategy generates a handful of trades.
    All intra-bar ranges are synthesised from per-bar noise so that
    high >= close >= low and open = previous close.
    """
    rng = np.random.default_rng(seed)
    n = days * 24 * 60
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")

    # Small positive drift so daily EMA trend is met some of the time
    log_ret = rng.normal(0.001 / 1440, 0.0003, n)
    close = 30_000.0 * np.exp(np.cumsum(log_ret))

    noise = rng.uniform(0.0002, 0.0010, n)
    high = close * (1.0 + noise)
    low = close * (1.0 - noise)

    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    # ensure high >= open and low <= open
    high = np.maximum(high, open_)
    low = np.minimum(low, open_)

    volume = rng.uniform(0.5, 5.0, n) * 10.0

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Module-level fixtures (built once)
# ---------------------------------------------------------------------------

_M1 = make_synthetic_m1(days=30, seed=42)
_H1 = add_trendpullback_features(resample_ohlcv(_M1, "60min"))
_D1 = add_daily_trend_gate(resample_ohlcv(_M1, "1D"))

_START = "2024-01-01"
_END   = "2024-01-30"


# ---------------------------------------------------------------------------
# Test A: run_backtest returns sane metrics on a short window
# ---------------------------------------------------------------------------

class TestBacktestSaneMetrics(unittest.TestCase):
    """run_backtest must return a dict with trades >= 0 and bounded metrics."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_backtest(_M1, _H1, _D1, _START, _END)

    def test_result_is_dict(self):
        self.assertIsInstance(self.result, dict)

    def test_trades_key_present(self):
        self.assertIn("trades", self.result)

    def test_trades_nonneg(self):
        self.assertGreaterEqual(self.result["trades"], 0)

    def test_win_rate_in_unit_interval(self):
        if self.result["trades"] == 0:
            self.skipTest("No trades generated on synthetic window")
        self.assertGreaterEqual(self.result["win_rate"], 0.0)
        self.assertLessEqual(self.result["win_rate"], 1.0)

    def test_mdd_in_unit_interval(self):
        if self.result["trades"] == 0:
            self.skipTest("No trades generated on synthetic window")
        self.assertGreaterEqual(self.result["mdd"], 0.0)
        self.assertLessEqual(self.result["mdd"], 1.0)

    def test_profit_factor_nonneg_or_none(self):
        if self.result["trades"] == 0:
            self.skipTest("No trades generated on synthetic window")
        pf = self.result.get("pf")
        if pf is not None:
            self.assertGreaterEqual(pf, 0.0)

    def test_zero_trade_result_has_only_trades_key(self):
        """A trivially short window should hit the early-return path."""
        tiny = run_backtest(_M1, _H1, _D1, "2024-01-01", "2024-01-03")
        if tiny["trades"] == 0:
            self.assertEqual(set(tiny.keys()), {"trades"})


# ---------------------------------------------------------------------------
# Test B: anti-lookahead sentinel
# ---------------------------------------------------------------------------

class TestAntiLookahead(unittest.TestCase):
    """Backtest results must be identical when strictly-future d1 rows are mutated.

    Design
    ------
    * backtest window  : _START … CUTOFF
    * The latest d1 date the strategy can access for bars ≤ CUTOFF is
      CUTOFF.floor('D') − 1 day  =  2024-01-05.
    * We mutate every d1 row with date ≥ 2024-01-06 (10–100× scale factor).
    * Any lookahead into those rows would cause a detectable result divergence.
    """

    CUTOFF = "2024-01-20"
    # d1 dates >= 2024-01-20 are "future" w.r.t. the CUTOFF window:
    # the latest d1 date consulted for h1 bars ending at 2024-01-20 is 2024-01-19
    # (day = t.floor('D') − 1 day for t ≤ 2024-01-20 gives day ≤ 2024-01-19).
    _FUTURE_D1_START = pd.Timestamp("2024-01-20", tz="UTC")
    _TOL = 1e-10

    @classmethod
    def _make_mutated_d1(cls) -> pd.DataFrame:
        """Return a copy of _D1 with all rows >= _FUTURE_D1_START heavily perturbed."""
        d1 = _D1.copy()
        mask = d1.index >= cls._FUTURE_D1_START
        n = int(mask.sum())
        if n == 0:
            return d1
        rng = np.random.default_rng(999)
        cols = [c for c in ("open", "high", "low", "close", "ema20", "ema20_prev")
                if c in d1.columns]
        for col in cols:
            d1.loc[mask, col] = d1.loc[mask, col].values * rng.uniform(10.0, 100.0, n)
        return d1

    @classmethod
    def setUpClass(cls):
        d1_mutated = cls._make_mutated_d1()
        cls.r_orig    = run_backtest(_M1, _H1, _D1,       _START, cls.CUTOFF)
        cls.r_mutated = run_backtest(_M1, _H1, d1_mutated, _START, cls.CUTOFF)

    def test_trade_count_identical(self):
        self.assertEqual(
            self.r_orig.get("trades"),
            self.r_mutated.get("trades"),
            "Trade count changed after mutating future d1 rows — lookahead detected.",
        )

    def _assert_metric_identical(self, key: str):
        if self.r_orig.get("trades", 0) == 0:
            self.skipTest("No trades — metric comparison not applicable")
        v_orig    = self.r_orig.get(key)
        v_mutated = self.r_mutated.get(key)
        if v_orig is None and v_mutated is None:
            return
        self.assertIsNotNone(v_orig,    f"'{key}' missing from original result")
        self.assertIsNotNone(v_mutated, f"'{key}' missing from mutated result")
        self.assertAlmostEqual(
            v_orig, v_mutated, delta=self._TOL,
            msg=(
                f"'{key}' differs after mutating future d1 rows "
                f"({v_orig} vs {v_mutated}) — lookahead detected."
            ),
        )

    def test_win_rate_identical(self):
        self._assert_metric_identical("win_rate")

    def test_avg_ret_identical(self):
        self._assert_metric_identical("avg_ret")

    def test_mdd_identical(self):
        self._assert_metric_identical("mdd")


# ---------------------------------------------------------------------------
# TrendPullback v1.1.0-D1-L-MXFR1 — minimal schema + sane-metrics tests
# ---------------------------------------------------------------------------

from scripts.strategies.strategy_trendpullback_v1_1_0_d1_l_mxfr1 import (
    run_backtest as run_backtest_d1,
    _prepare_d1,
    STRATEGY_NAME as D1_STRATEGY_NAME,
)
from scripts.reporting.schema_builder import build_cost_settings, build_strategy_output
from nautilus_lab.backtest import Periods

# Build D1-ready fixtures from the same synthetic 1m data (built once)
_H1_D1 = add_trendpullback_features(resample_ohlcv(_M1, "60min"))
_D1_FULL = _prepare_d1(_M1)

_D1_START = "2024-01-01"
_D1_END   = "2024-01-30"


class TestTrendPullbackD1SaneMetrics(unittest.TestCase):
    """run_backtest_d1 returns a sane dict on a short synthetic window."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_backtest_d1(_M1, _H1_D1, _D1_FULL, _D1_START, _D1_END)

    def test_result_is_dict(self):
        self.assertIsInstance(self.result, dict)

    def test_trades_key_present(self):
        self.assertIn("trades", self.result)

    def test_trades_nonneg(self):
        self.assertGreaterEqual(self.result["trades"], 0)

    def test_win_rate_in_unit_interval(self):
        if self.result["trades"] == 0:
            self.skipTest("No trades on synthetic window")
        self.assertGreaterEqual(self.result["win_rate"], 0.0)
        self.assertLessEqual(self.result["win_rate"], 1.0)

    def test_mdd_in_unit_interval(self):
        if self.result["trades"] == 0:
            self.skipTest("No trades on synthetic window")
        self.assertGreaterEqual(self.result["mdd"], 0.0)
        self.assertLessEqual(self.result["mdd"], 1.0)

    def test_zero_trade_result_has_only_trades_key(self):
        tiny = run_backtest_d1(_M1, _H1_D1, _D1_FULL, "2024-01-01", "2024-01-03")
        if tiny["trades"] == 0:
            self.assertEqual(set(tiny.keys()), {"trades"})


class TestTrendPullbackD1SchemaOutput(unittest.TestCase):
    """build_strategy_output for D1-L strategy contains all canonical keys."""

    FAKE_METRICS = {
        "trades": 15, "win_rate": 0.53, "avg_ret": 0.004,
        "pf": 1.6, "equity": 1.08, "ann_return": 0.12,
        "ann_sharpe": 1.1, "ann_vol": 0.11, "mdd": 0.09,
    }

    def test_strategy_output_canonical_keys(self):
        periods = Periods(
            "2024-01-01", "2025-03-31",
            "2025-04-01", "2025-06-30",
            "2025-07-01", "2026-03-03",
        )
        cost_settings = build_cost_settings(
            fee=2.0, slippage=0.0, tax=0.0,
            round_trip_cost=2.0, unit="points",
        )
        fake_strict = {
            "protocol": "strict: train-select, validation-check, oos-once",
            "chosen_params": {"pull": 0.30, "bn": 5, "en": 20, "tstop": 6, "cost": 2.0},
            "train": dict(self.FAKE_METRICS),
            "validation": dict(self.FAKE_METRICS),
            "validation_cost_stress": {"2.0": dict(self.FAKE_METRICS)},
            "oos_final_once": dict(self.FAKE_METRICS),
        }
        out = build_strategy_output(
            strategy=D1_STRATEGY_NAME,
            instrument="MXFR1",
            data="Shioaji",
            periods=periods,
            cost_settings=cost_settings,
            strict_result=fake_strict,
        )
        for key in ("strategy", "instrument", "data", "sample_periods",
                    "cost_settings", "protocol", "chosen_params",
                    "train", "validation", "oos_final_once"):
            self.assertIn(key, out)
        self.assertEqual(out["strategy"], D1_STRATEGY_NAME)
        self.assertIn("train", out["sample_periods"])
        self.assertIn("oos", out["sample_periods"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
